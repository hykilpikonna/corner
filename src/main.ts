import {createApp, h} from 'vue'
import App from './App.vue'
import router from './scripts/router'
import {i18n} from "@/scripts/messages"
import '@fortawesome/fontawesome-free/css/all.min.css'
import Collapse from "@/components/Collapse.vue"
import BlogIndex from "@/components/BlogIndex.vue";
import Tag from "@/components/Tag.vue";

const app = createApp(App).use(router).use(i18n)
    .component('Collapse', Collapse)
    .component('BlogIndex', BlogIndex)
    .component('Dynamic', {
        props: ['template'],
        render() {
            return h({template: this.template})
        }
    })
    .component('Tag', Tag)

// app.config.performance = true
app.mount('#app')
