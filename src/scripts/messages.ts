import {createI18n} from "vue-i18n";

export const messages = {
    en: {
        nav: {
            home: 'Home',
            about: 'About',
            blog: 'Blog',
            life: '生活',
            projects: 'Projects',
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
