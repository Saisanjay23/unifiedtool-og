/**
 * PlatformIcon — Renders real SVG platform logos with official brand colors.
 * Usage: <PlatformIcon platform="facebook" size={24} />
 */

const icons = {
    facebook: (size, color) => (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="12" fill={color || '#1877F2'} />
            <path d="M16.67 15.47l.53-3.47h-3.33V9.87c0-.95.46-1.87 1.95-1.87h1.51V5.15s-1.37-.23-2.68-.23c-2.73 0-4.52 1.66-4.52 4.66V12H7v3.47h3.13V24h3.87v-8.53h2.67z" fill="#fff" />
        </svg>
    ),
    instagram: (size, color) => (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
            <defs>
                <linearGradient id="ig-grad" x1="0%" y1="100%" x2="100%" y2="0%">
                    <stop offset="0%" stopColor="#FFDC80" />
                    <stop offset="25%" stopColor="#F77737" />
                    <stop offset="50%" stopColor="#E1306C" />
                    <stop offset="75%" stopColor="#C13584" />
                    <stop offset="100%" stopColor="#833AB4" />
                </linearGradient>
            </defs>
            <rect width="24" height="24" rx="6" fill={color || 'url(#ig-grad)'} />
            <rect x="4" y="4" width="16" height="16" rx="4" stroke="#fff" strokeWidth="1.8" fill="none" />
            <circle cx="12" cy="12" r="3.8" stroke="#fff" strokeWidth="1.8" fill="none" />
            <circle cx="17.2" cy="6.8" r="1.2" fill="#fff" />
        </svg>
    ),
    twitter: (size, color) => (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="12" fill={color || '#000'} />
            <path d="M13.52 10.77L17.95 5.5h-1.05l-3.84 4.57L10.2 5.5H6l4.64 6.93L6 18.5h1.05l4.06-4.83L14.1 18.5h4.2l-4.78-7.73zm-1.44 1.71l-.47-.69L7.56 6.34h1.61l3.02 4.43.47.69 3.93 5.76h-1.61l-3.4-4.74z" fill="#fff" />
        </svg>
    ),
    youtube: (size, color) => (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
            <rect width="24" height="24" rx="6" fill={color || '#FF0000'} />
            <path d="M19.8 9.03a2.16 2.16 0 00-1.52-1.53C16.88 7.1 12 7.1 12 7.1s-4.88 0-6.28.4a2.16 2.16 0 00-1.52 1.53A22.67 22.67 0 003.8 12a22.67 22.67 0 00.4 2.97 2.16 2.16 0 001.52 1.53c1.4.4 6.28.4 6.28.4s4.88 0 6.28-.4a2.16 2.16 0 001.52-1.53c.27-1 .4-2 .4-2.97s-.13-1.97-.4-2.97z" fill={color || '#FF0000'} />
            <path d="M10 15l4.5-3L10 9v6z" fill="#fff" />
        </svg>
    ),
    telegram: (size, color) => (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="12" fill={color || '#26A5E4'} />
            <path d="M5.43 11.87l11.53-4.45c.53-.19 1 .13.82.95l-1.96 9.24c-.14.66-.53.82-1.08.51l-3-2.21-1.45 1.4c-.16.16-.3.3-.61.3l.22-3.06 5.57-5.03c.24-.22-.05-.34-.38-.13l-6.88 4.34-2.96-.93c-.64-.2-.66-.64.14-.95z" fill="#fff" />
        </svg>
    ),
    tiktok: (size, color) => (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
            <rect width="24" height="24" rx="6" fill={color || '#000000'} />
            <path d="M16.6 8.2c-.8-.5-1.3-1.3-1.4-2.2h-2.1v9.6c0 1.3-1 2.3-2.3 2.3s-2.3-1-2.3-2.3 1-2.3 2.3-2.3c.2 0 .5 0 .7.1v-2.2c-.2 0-.5-.1-.7-.1-2.5 0-4.5 2-4.5 4.5s2 4.5 4.5 4.5 4.5-2 4.5-4.5V11c.9.6 1.9 1 3 1V9.9c-.3 0-1.1-.2-1.7-.6v-1.1z" fill="#FE2C55" />
            <path d="M16.6 8.2c.7.5 1.6.8 2.5.8V7.2c-.5 0-1-.1-1.4-.4-.4-.3-.8-.7-1-1.2h-1.5v9.6c0 1.3-1 2.3-2.3 2.3-.5 0-.9-.1-1.3-.4-.5-.4-.9-1-.9-1.7 0-1.1.8-2 1.9-2.2v-2.2c-2.3.2-4.1 2.1-4.1 4.5 0 1.2.5 2.4 1.3 3.2.9.8 2 1.3 3.2 1.3 2.5 0 4.5-2 4.5-4.5V9.7c-.3.2-1.5.7-2.5.4v-1c.8-.2 1.4-.5 1.8-.8h-.7z" fill="#25F4EE" />
        </svg>
    ),
};

export default function PlatformIcon({ platform, size = 24, color }) {
    const renderIcon = icons[platform];
    if (!renderIcon) return <span style={{ fontSize: size }}>❓</span>;
    return renderIcon(size, color);
}

// Export a helper for getting platform colors
export const PLATFORM_COLORS = {
    facebook: '#1877F2',
    instagram: '#E1306C',
    twitter: '#000000',
    youtube: '#FF0000',
    telegram: '#26A5E4',
    tiktok: '#FE2C55',
};
