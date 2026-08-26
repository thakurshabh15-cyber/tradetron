import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    rules: {
      // Fires on the standard async data-fetch-in-effect and fetched-data→form-state
      // sync idioms used throughout this app (useApi, Settings, Watchlist, etc.).
      // These are intentional external-system synchronisations, not render cascades.
      // Tracked as warnings so genuine regressions still surface in lint output.
      'react-hooks/set-state-in-effect': 'warn',
    },
  },
])
