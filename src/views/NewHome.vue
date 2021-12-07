<template>
    <div id="NewHome">
        <canvas id="three"></canvas>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {start} from "@/animation/Home";

@Options({components: {}})
export default class NewHome extends Vue
{
    mounted(): void
    {
        start('three')
    }
}
</script>

<style lang="sass" scoped>
#three
    width: 100vw
    height: 100vh
    position: absolute
    top: 0
    left: 0
    z-index: -1
</style>
