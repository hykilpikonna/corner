<template>
    <div id="Others">
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import Collapse from "@/components/Collapse.vue";

@Options({components: {Collapse}})
export default class Others extends Vue
{


}
</script>

<style lang="sass" scoped>
#Others
    text-align: left
    width: 600px
    margin: 20px auto
</style>
