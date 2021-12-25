import {createApp} from 'vue'
import App from './App.vue'
import router from './scripts/router'
import {i18n} from "@/messages";
import '@fortawesome/fontawesome-free/css/all.min.css'

const app = createApp(App).use(router).use(i18n)
// app.config.performance = true
app.mount('#app')
