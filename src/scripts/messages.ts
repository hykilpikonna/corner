import {createI18n} from "vue-i18n";

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
    zh: {
        // nav: {
        //     home: '温馨家',
        //     about: '关于我',
        //     blog: '部落格',
        //     projects: '我做的',
        // }
    }
}

export const i18n = createI18n({
    messages,
    locale: navigator.language.split('-')[0],
    fallbackLocale: 'en'
})

console.log(navigator.language)
