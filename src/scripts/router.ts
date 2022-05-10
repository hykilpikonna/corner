import {createRouter, createWebHashHistory, NavigationFailure, RouteRecordRaw} from 'vue-router'
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
        component: () => import('../views/About.vue')
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
        props: route => (route.query)
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
