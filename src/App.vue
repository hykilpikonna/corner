<template>
    <div id="nav" class="fbox-v font-code">
        <router-link class="router-link" to="/"><span>Home</span></router-link>
        <router-link class="router-link" to="/about"><span>About</span></router-link>
    </div>
    <router-view/>

    <MeruIcon/>
</template>

<script>
import MeruIcon from "@/components/MeruIcon";
import {Options, Vue} from "vue-class-component";

@Options({
    components: {MeruIcon}
})
export default class App extends Vue
{
}
</script>

<style lang="sass">
@import "css/global"

#nav
    position: absolute
    left: 20px
    height: 100vh
    font-size: 1.2em

    .router-link
        color: #80705c
        text-decoration: none
        writing-mode: vertical-rl
        text-orientation: sideways
        transform: scale(-1)

    .router-link.router-link-active span
        text-decoration: underline

    .router-link:before
        content: '·'
        margin-top: 10px
        margin-bottom: 10px

    .router-link:last-child:before
        content: none

#nav::before, #nav::after
    content: " "
    flex-grow: 1
</style>
