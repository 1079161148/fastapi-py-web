/* eslint-disable @typescript-eslint/no-explicit-any, @typescript-eslint/no-empty-object-type -- Vite 官方模板的标准 .vue 模块声明，泛型占位无法避免 */
/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}
