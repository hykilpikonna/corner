import {createRouter, createWebHistory, NavigationFailure, RouteRecordRaw} from 'vue-router'
import Home from '../views/Home.vue'

const routes: Array<RouteRecordRaw> = [
    {
        path: '/',
        name: 'Home',
        meta: {title: 'Home', nav: true},
        component: Home
    },
    {
        path: '/about',
        name: 'About',
        meta: {title: '关于', nav: true},
        component: () => import('../views/About.vue')
    },
    {
        path: '/life',
        name: 'Life',
        meta: {title: '生活', nav: true},
        component: () => import('../views/Life.vue')
    },
    {
        path: '/blog',
        name: 'Blog',
        meta: {title: '记事本', nav: true},
        component: () => import('../views/Blog.vue'),
        props: route => (route.query)
    },
    {
        path: '/photo',
        name: 'Photo',
        meta: {title: '相册', nav: true},
        component: () => import('../views/Photo.vue')
    },
    {
        path: '/photo/:id',
        name: 'Photo ID',
        meta: {title: '相册', navBookmark: 'Photo'},
        component: () => import('../views/Photo.vue')
    },
    {
        path: '/others',
        name: 'Others',
        meta: {title: '更多', nav: true},
        component: () => import('../views/Others.vue')
    },
    {
        path: '/kitchen-menu',
        name: 'Menu',
        meta: {title: '菜单', navBookmark: 'Others'},
        component: () => import('../views/others/Menu.vue')
    },
    {
        path: '/friends',
        name: 'Friends',
        meta: {title: '朋友们', navBookmark: 'Others'},
        component: () => import('../views/others/Friends.vue')
    },
    {
        path: '/projects',
        name: 'Projects',
        meta: {title: '我做的', navBookmark: 'Others'},
        component: () => import('../views/others/Projects.vue')
    },
]

export const router = createRouter({
    history: createWebHistory(),
    routes
})

export function pushQuery(query: {[id: string]: string | null}): Promise<void | NavigationFailure | undefined>
{
    const queries = {...router.currentRoute.value.query ?? {}}

    console.log(query)

    for (const k of Object.keys(query))
    {
        if (query[k] == null) delete queries[k]
        else queries[k] = query[k]
    }

    return router.push({query: queries})
}

export default router
