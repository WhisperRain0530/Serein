"""Run the model-selected jobs and the model-free Arc revision check."""
import asyncio
import logging
from .deployment import read_settings, configured_models, available_task_model
from .core.store import encode


async def run(application):
    from .compat.jobs import BackgroundJobs
    from .extensions.pipeline import scheduled_advance, ROLES
    async def pipeline():
        while True:
            try:
                await scheduled_advance(settings.database)
            except Exception:
                logging.getLogger(__name__).exception('Event pipeline failed; originals remain pending')
            await asyncio.sleep(30)
    running={}
    settings=application.settings
    try:
        while True:
            config=read_settings(settings.database)
            for feature in ('relations','dreams','narrative_revision','event_pipeline','operit_tagging','arc_linker'):
                if feature in application.enabled_extensions or (feature == 'narrative_revision' and
                        'narrative_scout' in application.enabled_extensions):continue
                selected=config['assignments'].get(feature)
                model=available_task_model(settings.database,feature) if selected else None
                signature=encode(model) if model else ''
                if feature=='narrative_revision':
                    signature='program-check'
                if feature=='event_pipeline':
                    selected_models=[item for item in configured_models(config) if item['id'] in
                                     [config['assignments'].get(role) for role in ROLES]]
                    signature=encode(selected_models) if selected_models else ''
                previous=running.get(feature)
                if previous and (previous[0]!=signature or previous[1].done()):
                    previous[1].cancel();await asyncio.gather(previous[1],return_exceptions=True)
                    del running[feature]
                if signature and feature not in running:
                    try:
                        if feature=='event_pipeline':
                            coroutine=pipeline()
                        elif feature=='operit_tagging':
                            from .import_tagging import run as tag_imports
                            coroutine=tag_imports(settings)
                        elif feature=='arc_linker':
                            from .arc_linking import run as link_arcs
                            coroutine=link_arcs(settings)
                        else:
                            coroutine=BackgroundJobs(settings,features={feature}).run()
                        running[feature]=(signature,asyncio.create_task(coroutine,name='configured:'+feature))
                    except Exception:
                        logging.getLogger(__name__).exception('Configured task could not start: %s',feature)
            await asyncio.sleep(5)
    finally:
        for _,task in running.values():task.cancel()
        await asyncio.gather(*(task for _,task in running.values()),return_exceptions=True)
