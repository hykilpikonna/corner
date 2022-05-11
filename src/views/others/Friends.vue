<template>
  <div id="Friends" class="general-page">
    <div class="title">
      <h2>朋友们</h2>
      <div class="subtitle">和他们的博客链接</div>
    </div>
  </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';

@Options({components: {}})
export default class Friends extends Vue
{
}
</script>

<style lang="sass" scoped>
@import "src/css/colors"
@import "src/css/responsive"
</style>
