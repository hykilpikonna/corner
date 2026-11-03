<template>
  <div class="four-oh-four">
    <div class="four1">Four</div>
    <div class="oh">Oh</div>
    <div class="four2">Four</div>
  </div>
</template>

<script setup lang="ts">
import type {NuxtError} from '#app'

defineProps<{ error: NuxtError }>()
</script>

<style lang="sass">
body:has(.four-oh-four)
  height: 100%
  pointer-events: none
  user-select: none

  #__nuxt, .four-oh-four
    height: 100%

  #nav, #nav-bookmark, #nav-background, #meru
    display: none
</style>

<style scoped lang="sass">
.four-oh-four
  display: flex
  align-items: center
  justify-content: center
  flex-direction: column
  font-family: "Microsoft YaHei UI", Avenir, Helvetica, Arial, sans-serif
  font-size: 20em
  text-transform: uppercase
  margin: 0
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' version='1.1' height='50px' width='50px'><text x='0' y='15' font-family='Avenir, Helvetica, Arial, sans-serif' fill='lightgray' transform='translate(15, 15) rotate(-45)' font-size='10'>肆零肆</text></svg>")

  .four2, .oh
    margin-top: -300px

  .four1
    margin-left: -100px
    color: #ff8373

  .oh
    color: khaki

  .four2
    margin-right: -100px
    color: cornflowerblue
</style>
