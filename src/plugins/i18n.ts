import {createI18n} from 'vue-i18n'
import {messages} from '@/scripts/messages'

export default defineNuxtPlugin((nuxtApp) => {
  const i18n = createI18n({
    legacy: false,
    messages,
    locale: 'en',
    fallbackLocale: 'en',
  })

  nuxtApp.vueApp.use(i18n)
})
