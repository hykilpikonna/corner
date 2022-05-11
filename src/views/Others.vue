<template>
  <div id="Others" class="general-page">
    <div class="title"><h2>更多链接</h2></div>
    <router-link class="rlink" to="/kitchen-menu">🍳 小桂桂的私房菜 菜单</router-link>
    <router-link class="rlink" to="/friends">🎎 朋友们</router-link>
  </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';

@Options({})
export default class Others extends Vue
{

}
</script>

<style lang="sass" scoped>
@import "src/css/colors"
@import "src/css/responsive"

a
  color: $color-text-main
  display: block
  margin-bottom: 0.5em
</style>
