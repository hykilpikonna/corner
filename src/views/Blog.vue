<template>
  <div id="Blog" class="general-page" v-if="meta">
    <div class="title">
      <h2>记事本</h2>
      <div class="subtitle">一些值得留下来的文字</div>
    </div>
    <div id="breadcrumb">
      <span class="clickable" @click="clearQuery">索引</span>
      <span v-if="tag">🏷️{{tag}}</span>
      <span v-if="category">📂{{category}}</span>
      <span class="no-after" v-if="post && activePost">{{activePost.title}}</span>
    </div>
    <BlogPostPreview v-for="m of filteredPosts" :key="m.id" :meta="m" :active="m === activePost"/>
  </div>
  <Loading v-else></Loading>
</template>

<script setup lang="ts">
import BlogPostPreview from "@/components/BlogPost.vue";
import Loading from "@/components/Loading.vue";
import type {BlogMeta} from "@/scripts/models";
import {ref, computed, onMounted} from "vue";
import type {Ref} from "vue";
import {hosts} from "@/scripts/constants";
import {globals} from "@/scripts/global";

const route = useRoute()
const router = useRouter()

let meta: Ref<BlogMeta> = ref({tags: [], categories: [], posts: []})

onMounted(() => {
  fetch(`${hosts.content}/content/generated/metas.json`).then(it => it.json()).then(it => {
    meta.value = it
    globals.staticMeta = it
  })
})

const post = computed(() => typeof route.query.post === 'string' ? route.query.post : undefined)
const category = computed(() => typeof route.query.category === 'string' ? route.query.category : undefined)
const tag = computed(() => typeof route.query.tag === 'string' ? route.query.tag : undefined)

const clearQuery = (): Promise<void> => router.push({query: {}})

const filteredPosts = computed(() => {
  const posts = meta.value.posts.filter(it => it.pinned || (tag.value ? it.tags.includes(tag.value) :
    category.value ? it.category == category.value : true))

  posts.sort((a, b) => (b.pinned ?? 0) - (a.pinned ?? 0))

  return posts
})

const activePost = computed(() => {
  const posts = filteredPosts.value
  if (posts.length == 0) return null
  if (!post.value) return null
  return post.value ? posts.filter(it => it.url_name == post.value)[0] : posts[0].pinned ? posts[0] : null
})
</script>

<style lang="sass" scoped>
@use "../css/colors"
@use "../css/responsive"

#breadcrumb
  color: colors.$color-text-light
  margin-bottom: 20px

  span:not(.no-after):after
    content: ">"
    margin: 0 10px

</style>
