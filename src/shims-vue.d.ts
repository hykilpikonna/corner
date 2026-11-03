/// <reference types="vite/client" />

/* eslint-disable */
declare module '*.vue'
{
  import type {DefineComponent} from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

declare const __BUILD_TIME__: string
declare const __GIT_COMMIT__: string
