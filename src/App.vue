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
            <router-link class="router-link" to="/new-home">
                <svg focusable="false" data-prefix="fal" data-icon="house-night" role="img" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 512" class="svg-inline--fa fa-house-night fa-w-20"><path fill="currentColor" d="M112,224a111.5,111.5,0,0,0,87-41.45,20.51,20.51,0,0,0-19.75-33.08A59.2,59.2,0,0,1,138.84,39.85a20.3,20.3,0,0,0,10.07-21.27,20.26,20.26,0,0,0-16.47-16.7A136,136,0,0,0,112,0a112,112,0,0,0,0,224ZM97.78,33.27a91.21,91.21,0,0,0,54.47,147.9A80,80,0,1,1,97.78,33.27Zm97.15,35.51,39.72,16.56,16.56,39.72a5.33,5.33,0,0,0,9.55,0l16.56-39.72L317,68.78a5.33,5.33,0,0,0,0-9.54L277.32,42.68,260.76,3a5.33,5.33,0,0,0-9.55,0L234.65,42.68,194.93,59.24a5.34,5.34,0,0,0,0,9.54ZM157,379.24l-39.72-16.57L100.76,323a5.34,5.34,0,0,0-9.55,0L74.65,362.67,34.93,379.24a5.34,5.34,0,0,0,0,9.54l39.72,16.56,16.56,39.72a5.33,5.33,0,0,0,9.55,0l16.56-39.72L157,388.78a5.33,5.33,0,0,0,0-9.54Zm179-101.9v85.33A21.39,21.39,0,0,0,357.36,384h85.31A21.39,21.39,0,0,0,464,362.67V277.34A21.4,21.4,0,0,0,442.67,256H357.36A21.4,21.4,0,0,0,336,277.34ZM368,288H432v64H368Zm266.49,8L576,244.75V144a16,16,0,0,0-32,0v72.75L410.53,100a16,16,0,0,0-21.07,0l-224,196a16,16,0,0,0,21.07,24.09L224,287.28V464a48.05,48.05,0,0,0,48,48H528a48.06,48.06,0,0,0,48-48V287.28l37.46,32.78A16,16,0,0,0,634.53,296ZM544,464a16,16,0,0,1-16,16H272a16,16,0,0,1-16-16V264a15.94,15.94,0,0,0-.81-4L400,133.27l144,126Z" class=""></path></svg></router-link>
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
    z-index: 2

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

    svg
        display: inline-block
        height: 1em
        transform: rotate(180deg)
        *
            box-sizing: inherit

// Tablet layout
@media screen and (max-width: 800px)
    #nav #menu
        opacity: 1

    #nav.home
        #menu, #nav-background
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
@media screen and (max-width: 370px)
    #nav #menu
        opacity: unset

    #nav.home
        #menu, #nav-background
            opacity: unset

    #nav.open
        #items, #nav-bookmark, #nav-background, #meru
            opacity: 1
            animation: fade-in-left .5s cubic-bezier(0.250, 0.460, 0.450, 0.940) both

    #nav
        #items, #nav-bookmark, #nav-background, #meru
            opacity: 0
            animation: fade-out-left 1s 0.5s cubic-bezier(0.250, 0.460, 0.450, 0.940) both

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
