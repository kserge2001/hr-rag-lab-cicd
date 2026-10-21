"""Check retrieval quality and answer consistency on the answers the live API test captured.

    python ci/quality_checks.py

Both checks are deterministic: no model grades anything. They use the evaluation Lambda,
which runs the app's own retrieval and Titan embeddings inside AWS.

1. Retrieval quality. For every case with an expected source policy, retrieval runs again
   as that case's test account. The expected policy must be among the passages returned.
     hit rate (recall@k)     share of cases whose expected policy was returned at all
     mean reciprocal rank    1 when it is always first, 0.5 when always second, and so on
2. Semantic consistency. Each policy question (a case with a source) is asked twice. The two
   answers must mean the same thing: their embedding similarity must reach MIN_CONSISTENCY.
   Refusals are left out: two correct refusals worded differently score low on embeddings
   (0.22 to 0.69 in testing), and the fact checks already make sure both runs refuse.

Writes build/quality.json for the report. Exits with an error if either check fails.
"""
import json
import os
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
MIN_CONSISTENCY = float(os.getenv('EVAL_MIN_CONSISTENCY', '0.75'))
BATCH = 10   # questions or answer pairs per Lambda call, well inside its time limit


def call_evaluation_lambda(payload):
    client = boto3.client('lambda', region_name=os.getenv('AWS_REGION', 'us-east-1'),
                          config=Config(read_timeout=300, retries={'max_attempts': 0}))
    result = client.invoke(FunctionName=os.environ['HR_EVALUATION_FUNCTION'], Payload=json.dumps(payload).encode())
    body = json.loads(result['Payload'].read())
    if result.get('FunctionError'):
        raise SystemExit('The evaluation Lambda failed: ' + str(body.get('errorType', 'unknown error')))
    return body


def in_batches(items):
    for start in range(0, len(items), BATCH):
        yield items[start:start + BATCH]


def retrieval_quality(cases):
    """For each case with a source: was the expected policy retrieved, and at what rank?"""
    with_source = [case for case in cases if case.get('source')]
    passages_by_question = []
    for batch in in_batches(with_source):
        questions = [{'question': case['question'], 'email': case['email']} for case in batch]
        passages_by_question += call_evaluation_lambda({'retrieve': questions})

    results = []
    for case, retrieved in zip(with_source, passages_by_question):
        rank = None
        for passage in retrieved['passages']:
            if passage['title'] == case['source']:
                rank = passage['rank']
                break
        results.append({'question': case['question'], 'expected': case['source'], 'rank': rank,
                        'returned': len(retrieved['passages'])})

    hits = 0
    reciprocal_rank_total = 0
    for result in results:
        if result['rank']:
            hits += 1
            reciprocal_rank_total += 1 / result['rank']
    if results:
        hit_rate = round(hits / len(results), 3)
        mean_reciprocal_rank = round(reciprocal_rank_total / len(results), 3)
    else:
        hit_rate = 1.0
        mean_reciprocal_rank = 1.0
    return {
        'cases': len(results),
        'hit_rate': hit_rate,
        'mean_reciprocal_rank': mean_reciprocal_rank,
        'misses': [result for result in results if not result['rank']],
        'results': results,
    }


def answer_consistency(cases, records):
    """For each case asked twice: how close in meaning were the two answers?"""
    answers = {}
    for record in records:
        if record.get('answer'):
            answers.setdefault(record['case_id'], []).append(record['answer'])

    pairs = []
    case_ids = []
    for case_id, texts in answers.items():
        if not cases[int(case_id)].get('source'):
            continue   # refusals and small talk: the fact checks cover them
        if len(texts) >= 2:
            pairs.append([texts[0], texts[1]])
            case_ids.append(case_id)

    scores = []
    for batch in in_batches(pairs):
        scores += call_evaluation_lambda({'similarity': batch})

    results = []
    for case_id, score in zip(case_ids, scores):
        question = cases[int(case_id)]['question']
        results.append({'case_id': case_id, 'question': question, 'similarity': score,
                        'passed': score >= MIN_CONSISTENCY})
    return {
        'cases': len(results),
        'threshold': MIN_CONSISTENCY,
        'lowest': min((result['similarity'] for result in results), default=1.0),
        'failures': [result for result in results if not result['passed']],
        'results': results,
    }


def main():
    report = json.loads((ROOT / 'build' / 'evaluation.json').read_text())
    cases = json.loads((ROOT / 'lambda' / 'data' / 'evaluation_questions.json').read_text(encoding='utf-8'))

    retrieval = retrieval_quality(cases)
    consistency = answer_consistency(cases, report['records'])
    latencies = sorted(record['latency_seconds'] for record in report['records'] if 'latency_seconds' in record)
    latency = {'answers': len(latencies), 'limit': float(os.getenv('EVAL_MAX_LATENCY_SECONDS', '20'))}
    if latencies:
        latency['median'] = round(latencies[len(latencies) // 2], 2)
        latency['p95'] = round(latencies[max(0, int(len(latencies) * 0.95) - 1)], 2)
        latency['max'] = round(latencies[-1], 2)

    summary = {'retrieval': retrieval, 'consistency': consistency, 'latency_seconds': latency}
    (ROOT / 'build' / 'quality.json').write_text(json.dumps(summary, indent=2))

    print(f"Retrieval: expected policy returned for {retrieval['cases'] - len(retrieval['misses'])} of "
          f"{retrieval['cases']} cases (hit rate {retrieval['hit_rate']}), mean reciprocal rank "
          f"{retrieval['mean_reciprocal_rank']}.")
    print(f"Consistency: lowest similarity between two answers to the same question "
          f"{consistency['lowest']:.3f} (threshold {MIN_CONSISTENCY}).")
    for miss in retrieval['misses']:
        print(f"  retrieval miss: expected {miss['expected']!r}")
    for failure in consistency['failures']:
        print(f"  inconsistent: case {int(failure['case_id']) + 1} scored {failure['similarity']:.3f}")

    if retrieval['misses'] or consistency['failures']:
        raise SystemExit('Retrieval or consistency check failed; see build/quality.json')


if __name__ == '__main__':
    main()
