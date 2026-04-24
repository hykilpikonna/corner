import { reactive } from 'vue';
import { BlogMeta } from "@/scripts/models";

export const globals = reactive({
  staticMeta: { tags: [], categories: [], posts: [] } as BlogMeta,
});
