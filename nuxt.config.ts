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
      script: [
        {
          src: 'https://cdnjs.cloudflare.com/ajax/libs/jquery/3.6.0/jquery.min.js',
          integrity: 'sha512-894YE6QWD5I59HgZOGReFYm4dnWc1Qt5NtvYSaNcOP+u1T9qYdvdihz0PPSiiqn/+/3e7Jo4EaG7TubfWGUrMQ==',
          crossorigin: 'anonymous',
          referrerpolicy: 'no-referrer',
        },
        {
          src: 'https://cdnjs.cloudflare.com/ajax/libs/jqueryui/1.12.1/jquery-ui.min.js',
          integrity: 'sha512-uto9mlQzrs59VwILcLiRYeLKPPbS/bT71da/OEBYEwcdNUk8jYIy+D176RYoop1Da+f9mvkYrmj5MCLZWEtQuA==',
          crossorigin: 'anonymous',
          referrerpolicy: 'no-referrer',
        },
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
