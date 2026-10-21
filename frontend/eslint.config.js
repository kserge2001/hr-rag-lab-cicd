// Lint rules for the React app. Run with: npm run lint
import js from '@eslint/js'
import globals from 'globals'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'

export default [
  { ignores: ['dist/'] },                       // built files, not source
  js.configs.recommended,                       // likely bugs in plain JavaScript
  react.configs.flat.recommended,               // React mistakes, such as a list without keys
  react.configs.flat['jsx-runtime'],            // React 18 needs no `import React` in each file
  reactHooks.configs.flat.recommended,          // hooks called in the right order
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: { globals: globals.browser },
    settings: { react: { version: 'detect' } },
    rules: { 'react/prop-types': 'off' },       // this app does not use prop-types
  },
  { files: ['vite.config.js'], languageOptions: { globals: globals.node } },   // runs in Node, not the browser
]
