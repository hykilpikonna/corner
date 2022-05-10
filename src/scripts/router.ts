import {createRouter, createWebHashHistory, NavigationFailure, RouteRecordRaw} from 'vue-router'
import Home from '../views/Home.vue'

const routes: Array<RouteRecordRaw> = [
    {
        path: '/',
        name: 'Home',
        meta: {title: 'Home', nav: true},
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
        meta: {title: '关于', nav: true},
        // route level code-splitting
        // this generates a separate chunk (about.[hash].js) for this route
        // which is lazy-loaded when the route is visited.
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
