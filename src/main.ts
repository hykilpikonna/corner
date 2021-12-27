import {createApp, h} from 'vue'
import App from './App.vue'
import router from './scripts/router'
import {i18n} from "@/scripts/messages"
import '@fortawesome/fontawesome-free/css/all.min.css'
import Collapse from "@/components/Collapse.vue"
import BlogIndexTags from "@/components/BlogIndexTags.vue";
import BlogIndexCategories from "@/components/BlogIndexCategories.vue";

const app = createApp(App).use(router).use(i18n)
    .component('Collapse', Collapse)
    .component('BlogIndexTags', BlogIndexTags)
    .component('BlogIndexCategories', BlogIndexCategories)
    .component('Dynamic', {
        props: ['template'],
        render() {
            return h({template: this.template})
        }
    })

// app.config.performance = true
app.mount('#app')
