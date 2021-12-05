import {createRouter, createWebHashHistory, RouteRecordRaw} from 'vue-router'
import Home from '../views/Home.vue'

const routes: Array<RouteRecordRaw> = [
    {
        path: '/',
        name: 'Home',
        component: Home
    },
    {
        path: '/about',
        name: 'About',
        // route level code-splitting
        // this generates a separate chunk (about.[hash].js) for this route
        // which is lazy-loaded when the route is visited.
        component: () => import(/* webpackChunkName: "about" */ '../views/About.vue')
    },
    {
        path: '/blog',
        name: 'Blog',
        component: () => import('../views/About.vue')
    },
    {
        path: '/projects',
        name: 'Projects',
        component: () => import('../views/About.vue')
    }
]

export const router = createRouter({
    history: createWebHashHistory(),
    routes
})

export default router
