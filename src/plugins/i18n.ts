import {createI18n} from 'vue-i18n'
import {messages} from '@/scripts/messages'

export default defineNuxtPlugin((nuxtApp) => {
  let locale = 'en'
  if (import.meta.server) {
    const acceptLanguage = useRequestHeaders(['accept-language'])['accept-language']
    locale = acceptLanguage?.split(',')[0]?.split(';')[0]?.split('-')[0] || 'en'
  } else if (import.meta.client) {
    locale = navigator.language.split('-')[0] || 'en'
  }

  const i18n = createI18n({
    legacy: false,
    messages,
    locale,
    fallbackLocale: 'en',
  })

  nuxtApp.vueApp.use(i18n)
})
