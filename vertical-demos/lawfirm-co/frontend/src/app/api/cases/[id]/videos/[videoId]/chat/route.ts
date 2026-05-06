import { NextRequest, NextResponse } from 'next/server';

export const maxDuration = 300; // 5 minutes
export const dynamic = 'force-dynamic';

export async function POST(
    request: NextRequest,
    props: { params: Promise<{ id: string; videoId: string }> }
) {
    const params = await props.params;
    try {
        const body = await request.json();
        const backendUrl = process.env.BACKEND_URL || 'http://backend:8000';

        const response = await fetch(
            `${backendUrl}/cases/${params.id}/videos/${params.videoId}/chat`,
            {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(body),
                // No timeout - let it run for the full maxDuration
            }
        );

        const data = await response.json();

        if (!response.ok) {
            return NextResponse.json(data, { status: response.status });
        }

        return NextResponse.json(data);
    } catch (error: any) {
        console.error('Video chat API error:', error);
        return NextResponse.json(
            { detail: error.message || 'Internal server error' },
            { status: 500 }
        );
    }
}
