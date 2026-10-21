"""Public-safe quality report; never posts raw questions, answers, or employee names."""
import json
import math
from pathlib import Path

def wilson(passed, total):
    if total <= 0 or not 0 <= passed <= total:
        raise ValueError('Invalid counts')
    z = 1.96
    p = passed / total
    denom = 1 + z*z/total
    centre = (p + z*z/(2*total))/denom
    half = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total))/denom
    return centre-half, centre+half

def summarize(data, quality=None, deepeval=None):
    """The report text. quality and deepeval are the results of the other checks, when they ran."""
    rows = data['records']
    if len(rows) != data['expected'] or not rows:
        raise ValueError('Incomplete report')
    # Collapse correlated repetitions to cases. A case passes only if all its runs pass.
    grouped = {}
    for row in rows:
        grouped.setdefault(row['case_id'], []).append(row['passed'])
    passed = sum(all(runs) for runs in grouped.values())
    lo, hi = wilson(passed, len(grouped))
    heading = (f'## HR application evaluation\n\n'
        f'Cases passing every repetition: {passed}/{len(grouped)}.\n\n'
        f'Illustrative 95% Wilson interval: {lo:.1%}–{hi:.1%}. '
        'This assumes independent representative cases; this fixed regression set '
        'does not establish a production error rate.\n\n'
        f'Application requests recorded: {len(rows)}.\n\n')
    sections, other_checks_passed = quality_section(quality, deepeval)
    if all(r['passed'] for r in rows) and other_checks_passed:
        verdict = 'PASS'
    else:
        verdict = 'FAIL'
    return heading + sections + f'Release gate: {verdict}.\n'


def quality_section(quality, deepeval):
    """Counts and scores only: no questions, answers or names. Returns (text, all_passed)."""
    lines = []
    passed = True
    if quality:
        latency = quality['latency_seconds']
        retrieval = quality['retrieval']
        consistency = quality['consistency']
        lines.append('### Speed, retrieval and consistency\n')
        if 'max' in latency:
            lines.append(f"- Latency: median {latency['median']} s, p95 {latency['p95']} s, "
                         f"slowest {latency['max']} s (limit {latency['limit']:.0f} s per answer).")
        found = retrieval['cases'] - len(retrieval['misses'])
        lines.append(f"- Retrieval: expected policy returned for {found} of {retrieval['cases']} cases, "
                     f"mean reciprocal rank {retrieval['mean_reciprocal_rank']}.")
        lines.append(f"- Consistency: lowest similarity between two answers to the same question "
                     f"{consistency['lowest']:.2f} (minimum {consistency['threshold']}).")
        if retrieval['misses'] or consistency['failures']:
            passed = False
    if deepeval:
        lines.append('\n### Model-graded checks (DeepEval, sampled)\n')
        by_metric = {}
        for result in deepeval:
            by_metric.setdefault(result['metric'], []).append(result)
        for metric, results in by_metric.items():
            failures = sum(1 for result in results if not result['passed'])
            average = sum(result['score'] for result in results) / len(results)
            lines.append(f'- {metric}: average {average:.2f} over {len(results)} cases, {failures} failed.')
            if failures:
                passed = False
    if not lines:
        return '', passed
    return '\n'.join(lines) + '\n\n', passed

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    # The other checks' results, when their steps ran.
    optional = {}
    for name in ('quality', 'deepeval'):
        path = root / 'build' / f'{name}.json'
        if path.exists():
            optional[name] = json.loads(path.read_text())
    report = summarize(json.loads((root / 'build/evaluation.json').read_text()),
                       optional.get('quality'), optional.get('deepeval'))
    (root / 'build/report.md').write_text(report)
    print(report)
