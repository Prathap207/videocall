from fastapi import APIRouter

from app.routes import liveStreamRouter, usersRouter,videoCall

def mainRouter(app: APIRouter):
    app.include_router(usersRouter.router, tags=['users'])
    # app.include_router(streamRouter.router, tags=['stream'])
    app.include_router(liveStreamRouter.router, tags=['live-streaming'])

    app.include_router(videoCall.router, tags=['video-call'])
