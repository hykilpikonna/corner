<template>
    <table class="meta">
        <tr v-for="(v, k) in filteredTable" :key="k">
            <td>{{k}}</td>
            <td v-html="v"></td>
        </tr>
    </table>
</template>

<script setup lang="ts">
import {computed} from 'vue'

const props = defineProps<{ table: {[id: string]: unknown} }>()

const filteredTable = computed((): {[id: string]: unknown} => {
    const t: {[id: string]: unknown} = {}

    Object.keys(props.table).forEach(k => {
        if (!props.table[k]) return

        let newK = k.replace(/([A-Z])/g, " $1")
        newK = newK.charAt(0).toUpperCase() + newK.slice(1)
        t[newK] = props.table[k]
    })

    return t
})
</script>

<style lang="sass" scoped>
@use "../css/colors"

.meta
    td:first-child
        text-align: right
        color: colors.$color-text-light

    td:last-child
        display: inline-block
        margin-left: 20px
</style>
