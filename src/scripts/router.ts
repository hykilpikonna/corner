export function pushQuery(query: {[id: string]: string | null}): void
{
    const url = new URL(window.location.href);
    
    for (const [key, value] of Object.entries(query)) {
        if (value === null) {
            url.searchParams.delete(key);
        } else {
            url.searchParams.set(key, value);
        }
    }
    
    window.history.pushState({}, '', url);
    
    // Dispatch a custom event so other components (like Navigation) can react if they need to
    window.dispatchEvent(new Event('popstate'));
}

export const router = {
    install: (app: any) => {
        app.config.globalProperties.$router = router;
    },
    push: (to: any) => {
        if (typeof to === 'string') {
            window.location.href = to;
        } else if (to.path) {
            let url = to.path;
            if (to.query) {
                const params = new URLSearchParams(to.query);
                url += '?' + params.toString();
            }
            window.location.href = url;
        }
    }
};

export default router

