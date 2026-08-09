<template>
  <BlogView />
</template>

<script setup lang="ts">
import BlogView from '@/views/Blog.vue'

definePageMeta({title: '记事本', navBookmark: 'blog'})
</script>
