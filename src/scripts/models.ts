export interface BlogPost
{
  id: number
  title: string
  tags: string[]
  file: string
  date: string
  url_name: string

  content: string

  subtitle?: string
  title_image?: string
  category?: string
  pinned?: number
}

export interface BlogMeta
{
  // tags[i] = [Tag Name, Number of Blog Posts]
  tags: [string, number][]
  // categories[i] = [Category Name, Number of Posts]
  categories: [string, number][]
  posts: BlogPost[]
}
