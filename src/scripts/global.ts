import type {InjectionKey, Ref} from 'vue'
import type {BlogMeta} from '@/scripts/models'

export const emptyBlogMeta = (): BlogMeta => ({tags: [], categories: [], posts: []})
export const blogMetaKey: InjectionKey<Ref<BlogMeta>> = Symbol('blog-meta')
