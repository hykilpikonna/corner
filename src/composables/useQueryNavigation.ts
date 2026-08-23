export function useQueryNavigation() {
    const route = useRoute()
    const router = useRouter()

    const pushQuery = (query: Record<string, string | null>) => {
        const queries = {...route.query}

        for (const [key, value] of Object.entries(query)) {
            if (value === null) delete queries[key]
            else queries[key] = value
        }

        return router.push({query: queries})
    }

    return {pushQuery}
}
