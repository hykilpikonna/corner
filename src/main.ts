import {createApp} from 'vue'
import App from './App.vue'
import router from './router'
import {i18n} from "@/messages";
import '@fortawesome/fontawesome-free/css/all.min.css'

createApp(App).use(router).use(i18n).mount('#app')
