<template>
    <div id="nav">
        <router-link to="/">Home</router-link>
        |
        <router-link to="/about">About</router-link>
    </div>
    <router-view/>

    <MeruIcon/>
</template>

<style lang="sass">
@import "css/global"
</style>

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
