import {createRouter, createWebHashHistory, RouteRecordRaw} from 'vue-router'
import Home from '../views/Home.vue'

const routes: Array<RouteRecordRaw> = [
    {
        path: '/',
        name: 'Home',
        meta: {title: 'Home'},
        component: Home
    },
    {
        path: '/new-home',
        name: 'New Home',
        meta: {title: 'Home'},
        component: () => import('../views/NewHome.vue')
    },
    {
        path: '/about',
        name: 'About',
        meta: {title: 'About Me'},
        // route level code-splitting
        // this generates a separate chunk (about.[hash].js) for this route
        // which is lazy-loaded when the route is visited.
        component: () => import(/* webpackChunkName: "about" */ '../views/About.vue')
    },
    {
        path: '/life',
        name: 'Life',
        meta: {title: 'Life'},
        component: () => import('../views/Life.vue')
    },
    {
        path: '/projects',
        name: 'Projects',
        meta: {title: 'Projects'},
        component: () => import('../views/Projects.vue')
    },
    {
        path: '/blog',
        name: 'Blog',
        meta: {title: '记事本'},
        component: () => import('../views/Blog.vue'),
        props: route => ({ query: route.query.q })
    },
    {
        path: '/color',
        name: 'ColorPicker',
        meta: {title: 'Color Picker'},
        component: () => import('../components/color/ColorPickerTest.vue')
    },
]

export const router = createRouter({
    history: createWebHashHistory(),
    routes
})

export default router
