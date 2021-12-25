<template>
    <div class="collapse">
        <slot></slot>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';

@Options({components: {}})
export default class Collapse extends Vue
{

}
</script>

<style lang="sass">
.collapse
    display: none
</style>
