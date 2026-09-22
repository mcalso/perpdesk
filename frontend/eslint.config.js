// 前端静态检查。重点不是风格 —— 风格有 tsconfig 的 strict 系列兜着 ——
// 而是 tsc 看不见的那类错误，尤其是 react-hooks/rules-of-hooks：
// 本项目真的因为「提前 return 站在 hook 前面」让未配凭据的用户开持仓页
// 白屏过一次（ExchangeAccount），那次是靠冒烟测试事后抓到的，
// 这条规则能在写代码的当下就拦住。
import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks },
    rules: {
      // 只开这两条经典规则，不整套引入 recommended。
      // eslint-plugin-react-hooks@7 的 recommended 里带进了 React Compiler
      // 那套实验规则（purity / set-state-in-effect / refs），实测在本项目
      // 报 12 条，全部指向刻意为之的写法：effect 里取数是 Suspense 之前
      // 的标准做法，FlashCell 直接操作 DOM 是为了避开每秒 200 次重渲染。
      // 把它们打开只会换来一堆抑制注释。哪天真的上 React Compiler 再说。
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      // 未用变量：允许下划线前缀的占位参数（回调签名要求但用不上的那种）
      '@typescript-eslint/no-unused-vars': [
        'error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
  {
    // 冒烟脚本跑在 Node 里，不是浏览器
    files: ['smoke/**/*.mjs'],
    languageOptions: {
      globals: { console: 'readonly', process: 'readonly', setTimeout: 'readonly',
                 clearTimeout: 'readonly', URL: 'readonly', Buffer: 'readonly',
                 __dirname: 'readonly', fetch: 'readonly' },
    },
    rules: { 'no-undef': 'error' },
  },
)
