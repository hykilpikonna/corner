import {createI18n} from 'vue-i18n'
import {messages} from '@/scripts/messages'

// Ensure compile-time feature flags used by vue-i18n have runtime fallbacks
// during SSR prerender in production when externalized.
if (typeof (globalThis as any).__VUE_PROD_DEVTOOLS__ === 'undefined') {
  ;(globalThis as any).__VUE_PROD_DEVTOOLS__ = false
}

export default defineNuxtPlugin((nuxtApp) => {
  // Locale must match between prerendered HTML and the client, or hydration
  // mismatches. accept-language isn't available to prerendering (no request),
  // so resolve once from the client and pin via useState — which serializes
  // into the payload when SSR does run, keeping both sides in agreement.
  const locale = useState<string>('locale', () => {
    if (import.meta.client) return navigator.language.split('-')[0] || 'en'
    const acceptLanguage = useRequestHeaders(['accept-language'])['accept-language']
    return acceptLanguage?.split(',')[0]?.split(';')[0]?.split('-')[0] || 'en'
  })

  const i18n = createI18n({
    legacy: false,
    messages,
    locale: locale.value,
    fallbackLocale: 'en',
  })

  nuxtApp.vueApp.use(i18n)
})
