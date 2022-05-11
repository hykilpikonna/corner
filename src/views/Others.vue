<template>
  <div id="Others" class="general-page">
    <div class="title"><h2>更多链接</h2></div>
    <div class="links">
      <router-link class="rlink" to="/kitchen-menu">🍳 小桂桂的私房菜 菜单</router-link>
      <router-link class="rlink" to="/friends">🎎 朋友们</router-link>
      <a href="http://status.hydev.org/">🔌 看看服务器们过得怎么样</a>
    </div>
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

.links
  display: flex
  flex-direction: column
  align-items: flex-start

a
  color: $color-text-main
  margin-bottom: 0.5em
</style>
