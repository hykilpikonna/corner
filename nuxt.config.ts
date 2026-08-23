export default defineNuxtConfig({
  srcDir: 'src/',
  ssr: true,

  modules: ['@unocss/nuxt'],
  css: ['@fortawesome/fontawesome-free/css/all.min.css'],

  app: {
    head: {
      htmlAttrs: {lang: 'zh-CN'},
      meta: [
        {charset: 'utf-8'},
        {name: 'viewport', content: 'width=device-width, initial-scale=1'},
        {name: 'description', content: "Aza's Corner"},
      ],
      link: [
        {rel: 'apple-touch-icon', sizes: '180x180', href: '/icons/apple-touch-icon.png'},
        {rel: 'icon', type: 'image/png', sizes: '32x32', href: '/icons/favicon-32x32.png'},
        {rel: 'icon', type: 'image/png', sizes: '16x16', href: '/icons/favicon-16x16.png'},
      ],
    },
  },

  nitro: {
    prerender: {
      crawlLinks: true,
      routes: [
        '/',
        '/api/photos',
        '/about',
        '/life',
        '/blog',
        '/photo',
        '/others',
        '/kitchen-menu',
        '/friends',
        '/projects',
      ],
    },
  },

  hooks: {
    async 'nitro:config'(nitroConfig) {
      if (nitroConfig.dev) return

      try {
        const response = await fetch('https://p.aza.moe/photos')
        const photos = await response.json() as Array<{id: string}>
        nitroConfig.prerender ??= {}
        nitroConfig.prerender.routes ??= []
        nitroConfig.prerender.routes.push(...photos.map(photo => `/photo/${photo.id}`))
      } catch (error) {
        console.warn('Could not discover photo routes for prerendering:', error)
      }
    },
  },

  compatibilityDate: '2026-08-04',
})
