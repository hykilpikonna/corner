export default defineNuxtConfig({
  srcDir: 'src/',
  ssr: true,
  modules: ['@unocss/nuxt'],
  css: ['@fortawesome/fontawesome-free/css/all.min.css'],
  app: {
    head: {
      script: [
        {
          src: 'https://cdnjs.cloudflare.com/ajax/libs/jquery/3.6.0/jquery.min.js',
          integrity: 'sha512-894YE6QWD5I59HgZOGReFYm4dnWc1Qt5NtvYSaNcOP+u1T9qYdvdihz0PPSiiqn/+/3e7Jo4EaG7TubfWGUrMQ==',
          crossorigin: 'anonymous',
          referrerpolicy: 'no-referrer'
        },
        {
          src: 'https://cdnjs.cloudflare.com/ajax/libs/jqueryui/1.12.1/jquery-ui.min.js',
          integrity: 'sha512-uto9mlQzrs59VwILcLiRYeLKPPbS/bT71da/OEBYEwcdNUk8jYIy+D176RYoop1Da+f9mvkYrmj5MCLZWEtQuA==',
          crossorigin: 'anonymous',
          referrerpolicy: 'no-referrer'
        }
      ]
    }
  },
  nitro: {
    prerender: {
      crawlLinks: true,
      routes: ['/', '/about', '/life', '/blog', '/photo', '/others', '/kitchen-menu', '/friends', '/projects']
    }
  }
})
