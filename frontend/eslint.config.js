import pluginVue from 'eslint-plugin-vue'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import skipFormatting from '@vue/eslint-config-prettier/skip-formatting'

// @vue/eslint-config-typescript v14 起改为函数式导出（defineConfigWithVueTs），
// 它会自动为 .vue 文件接入类型信息；旧版 default 导出的展开写法已不可用。
export default defineConfigWithVueTs(
  { ignores: ['dist/**', 'node_modules/**', 'src/api/generated/**'] },
  ...pluginVue.configs['flat/recommended'],
  ...vueTsConfigs.recommendedTypeChecked,
  skipFormatting,
  {
    rules: {
      'vue/multi-word-component-names': 'off',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },
)
