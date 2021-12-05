<template>
    <div id="nav" class="fbox-v">
        <div id="hamburger"></div>
        <div class="spacer f-grow1"/>

        <router-link class="router-link" to="/projects">{{ $t('nav.projects') }}</router-link>
        <div class="dot">·</div>
        <router-link class="router-link" to="/blog">{{ $t('nav.blog') }}</router-link>
        <div class="dot">·</div>
        <router-link class="router-link" to="/about">{{ $t('nav.about') }}</router-link>
        <div class="dot">·</div>
        <router-link class="router-link" to="/">{{ $t('nav.home') }}</router-link>
        <div class="dot">·</div>

        <div id="nav-bookmark" ref="bookmark" :style="bookmarkCss"></div>
        <div id="nav-background"></div>

        <img id="meru" src="/meru_256px.png" alt="">
    </div>

    <router-view/>
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
    bookmarkUpdateIntervalId!: number
    lastTop = 0

    updateBookmark(): void
    {
        runAfter(() => document.querySelector('.router-link-active') != this.currentLink, () =>
        {
            let el = document.querySelector('.router-link-active')
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

        // Update every second
        this.bookmarkUpdateIntervalId = setInterval(this.calculateBookmarkCss, 1000)
    }

    calculateBookmarkCss(): void
    {
        // https://developer.mozilla.org/zh-CN/docs/Web/API/Element/getBoundingClientRect
        let box = this.currentLink.getBoundingClientRect()
        if (box.top == this.lastTop) return

        let h = box.bottom - box.top
        this.bookmarkCss = {
            top: `${box.top - 8}px`,
            border: `${Math.round(h / 2) + 8}px solid var(--bookmark-color)`,
            'border-right': '20px solid transparent',
            'border-left': '50px solid var(--bookmark-color)'
        }
        console.log(this.bookmarkCss)
    }
}
</script>

<style lang="sass">
@import "css/global"

#nav
    position: fixed
    left: 0
    height: 100vh
    font-size: 1.4em
    align-items: flex-start

    #nav-bookmark
        position: absolute
        left: 0
        width: 20px
        height: 0
        z-index: 5
        --bookmark-color: rgb(255, 225, 230)

    .router-link
        color: rgba(128, 112, 92, 0.71)
        position: relative
        z-index: 100

    .dot
        content: '·'
        margin: 20px 0

    .router-link, .dot
        text-decoration: none
        writing-mode: vertical-rl
        text-orientation: sideways
        transform: scale(-1)
        padding-right: 20px

    #nav-background
        position: absolute
        height: 100%
        width: 100px
        left: -40px
        background: linear-gradient(to right, rgba(247, 169, 121, 0.42), transparent)
        z-index: 10

    #meru
        height: 160px

// Phone layout
@media screen and (max-width: 570px)
</style>
