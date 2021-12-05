<template>
    <div id="MeruIcon">
        <img alt="meru" src="../../public/meru_256px.png">
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';

@Options({components: {}})
export default class MeruIcon extends Vue
{

}
</script>

<style lang="sass" scoped>
img
    width: 160px
    position: fixed
    left: 0
    bottom: 0
</style>
