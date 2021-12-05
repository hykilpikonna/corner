<template>
    <div id="nav" class="fbox-v">
        <router-link class="router-link" to="/"><span>{{ $t('nav.home') }}</span></router-link>
        <router-link class="router-link" to="/about"><span>{{ $t('nav.about') }}</span></router-link>
        <router-link class="router-link" to="/blog"><span>{{ $t('nav.blog') }}</span></router-link>
        <router-link class="router-link" to="/projects"><span>{{ $t('nav.projects') }}</span></router-link>

        <div id="nav-bookmark" ref="bookmark" :style="bookmarkCss"></div>
    </div>

    <router-view/>

    <MeruIcon/>
</template>

<script lang="ts">
import MeruIcon from "@/components/MeruIcon.vue";
import {Options, Vue} from "vue-class-component";
import router from "@/router";

function runAfter(condition: () => boolean, callback: () => void, timeout=10): void
{
    let times = 0
    let id = setInterval(() => {
        if (times <= timeout && !condition())
        {
            times ++
            return
        }

        callback()
        clearInterval(id)
    }, 1)
}

@Options({
    components: {MeruIcon}
})
export default class App extends Vue
{
    currentLink!: Element
    bookmarkCss: {[id: string]: string} = {}

    updateBookmark(): void
    {
        runAfter(() => document.querySelector('.router-link-active span') != this.currentLink, () =>
        {
            let el = document.querySelector('.router-link-active span')
            if (el != null) this.currentLink = el
            this.calculateBookmarkCss()
        })
    }

    mounted(): void
    {
        router.afterEach(() => this.updateBookmark())
        this.updateBookmark()

        // Resize listener
        window.addEventListener('resize', () => this.calculateBookmarkCss(), true);
    }

    calculateBookmarkCss(): void
    {
        // https://developer.mozilla.org/zh-CN/docs/Web/API/Element/getBoundingClientRect
        let box = this.currentLink.getBoundingClientRect()
        let h = box.bottom - box.top
        this.bookmarkCss = {
            top: `${box.top - 8}px`,
            border: `${Math.round(h / 2) + 8}px solid rgba(255, 216, 224, 0.49)`,
            'border-right': '20px solid transparent',
            'border-left': '50px solid rgba(255, 216, 224, 0.49)'
        }
        console.log(this.bookmarkCss)
    }
}
</script>

<style lang="sass">
@import "css/global"

#nav
    position: absolute
    left: 20px
    height: 100vh
    font-size: 1.4em
    flex-direction: column-reverse

    #nav-bookmark
        position: absolute
        left: -40px
        width: 30px
        height: 0
        z-index: 0

    .router-link
        color: rgba(128, 112, 92, 0.71)
        text-decoration: none
        writing-mode: vertical-rl
        text-orientation: sideways
        transform: scale(-1)

    .router-link:before
        content: '·'
        margin: 20px 0

    .router-link
        position: relative
        z-index: 10

#nav::after
    content: " "
    flex-grow: 1

#nav::before
    content: " "
    height: 180px
</style>
