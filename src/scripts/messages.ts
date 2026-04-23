export const messages = {
  en: {
    nav: {
      home: 'Home',
      about: '关于',
      blog: '记事本',
      life: '生活',
      projects: 'Projects',
      others: '更多',
      photo: '相册'
    }
  },
  zh: {}
}

export const resolveLocale = (): string => {
  if (typeof navigator === 'undefined') return 'en'
  return navigator.language.split('-')[0]
}
