import {createI18n} from 'vue-i18n'
import {messages, resolveLocale} from '@/scripts/messages'

export default defineNuxtPlugin((nuxtApp) => {
  const i18n = createI18n({
    messages,
    locale: resolveLocale(),
    fallbackLocale: 'en'
  })

  nuxtApp.vueApp.use(i18n)
})
