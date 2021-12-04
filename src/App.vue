<template>
    <div id="nav">
        <router-link class="router-link" to="/">Home</router-link>
        <router-link class="router-link" to="/about">About</router-link>
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

.router-link
    color: red
</style>
