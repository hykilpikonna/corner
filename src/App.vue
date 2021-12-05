<template>
    <div id="nav" class="fbox-v"
         :class="(this.currentLink ? this.currentLink.textContent.toLowerCase() : '') + ' ' + (menuOpen ? 'open' : '')">
        <div id="menu" @click="showMenu"><i class="fas fa-bars"></i></div>

        <div id="items" class="fbox-v">
            <router-link class="router-link" to="/projects">{{ $t('nav.projects') }}</router-link>
            <div class="dot">·</div>
            <router-link class="router-link" to="/blog">{{ $t('nav.blog') }}</router-link>
            <div class="dot">·</div>
            <router-link class="router-link" to="/about">{{ $t('nav.about') }}</router-link>
            <div class="dot">·</div>
            <router-link class="router-link" to="/">{{ $t('nav.home') }}</router-link>
            <div class="dot">·</div>
        </div>

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
    currentLink: Element = null as never as Element
    bookmarkCss = ""
    bookmarkUpdateIntervalId!: number
    lastTop = 0

    menuOpen = false

    showMenu(): void
    {
        this.menuOpen = !this.menuOpen

        // Auto close
        if (this.menuOpen)
        {
            setTimeout(() => this.menuOpen = false, 2000)
        }
    }

    updateBookmark(): void
    {
        // On page change
        runAfter(() => document.querySelector('.router-link-active') != this.currentLink, () =>
        {
            let el = document.querySelector('.router-link-active')
            if (el != null) this.currentLink = el
            this.calculateBookmarkCss()

            // Close menu
            this.menuOpen = false
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
        this.lastTop = box.top

        let h = box.bottom - box.top
        let width = Math.round(h / 2) + 8

        this.bookmarkCss = `top: ${box.top - 8}px;` +
            `border-width: ${width}px 20px ${width}px 50px;`
    }
}
</script>

<style lang="sass">
@import "css/global"
@import "css/animations"

#nav
    position: fixed
    left: 0
    height: 100vh
    font-size: 1.4em
    align-items: flex-start

    #menu
        padding: 5px 10px
        margin: 15px 10px
        background-color: rgba(255, 255, 255, 0.49)
        border-radius: 10px
        text-shadow: -1px -1px 1px rgba(255,255,255,.1), 1px 1px 1px rgba(0,0,0,.5)
        filter: drop-shadow(0 0 30px rgb(255, 255, 255))
        z-index: 100

        opacity: 0

    #nav-bookmark
        position: absolute
        left: 0
        width: 20px
        height: 0
        z-index: 5
        --bookmark-color: rgb(255, 225, 230)
        border-color: var(--bookmark-color) transparent var(--bookmark-color) var(--bookmark-color)
        border-style: solid
        border-width: 20px

    #items
        justify-content: flex-end
        z-index: 10

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
        left: 0
        $nav-bg-color: #f9f2e0
        //$nav-bg-color: rgb(255, 230, 214)
        border-left: 20px solid $nav-bg-color
        background: linear-gradient(to right, $nav-bg-color, transparent)
        z-index: 4
        pointer-events: none

    #meru
        height: 160px
        z-index: 100

// Tablet layout
@media screen and (max-width: 800px)
    #nav #menu
        opacity: 1

    #nav.home
        #nav-background
            opacity: 0

    #nav.home #menu
        opacity: 0

    #nav:not(.home).open
        #items, #nav-bookmark, #nav-background, #meru
            opacity: 1
            animation: fade-in-left .5s cubic-bezier(0.250, 0.460, 0.450, 0.940) both

    #nav:not(.home)
        #items, #nav-bookmark, #nav-background, #meru
            opacity: 0
            animation: fade-out-left 1s 0.5s cubic-bezier(0.250, 0.460, 0.450, 0.940) both

// Phone layout
@media screen and (max-width: 500px), (max-height: 660px)
    #app
        font-size: 14px

    #nav
        #nav-bookmark
            width: 10px

        #meru
            height: 120px

// Smaller phone layout
@media screen and (max-width: 400px)
    #nav
        .items
            opacity: 0
            animation: fade-out-left .5s cubic-bezier(0.250, 0.460, 0.450, 0.940) both

// Phone Height
@media screen and (max-height: 600px)
    #nav
        .dot
            margin: 10px 0

// Phone Height
@media screen and (max-height: 500px)
    #nav
        .router-link
            margin-bottom: 10px

        .dot
            display: none
</style>
