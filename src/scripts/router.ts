import {useRoute, useRouter} from 'vue-router'
import type {NavigationFailure} from 'vue-router'

export function pushQuery(query: {[id: string]: string | null}): Promise<void | NavigationFailure | undefined> {
  const route = useRoute()
  const router = useRouter()

  const queries = {...route.query}

  for (const k of Object.keys(query)) {
    if (query[k] == null) delete queries[k]
    else queries[k] = query[k]
  }

  return router.push({query: queries})
}
