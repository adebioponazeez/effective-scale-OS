class LocalTransport:
    async def call(self,target,method,*args,**kwargs):
        return await getattr(target,method)(*args,**kwargs)
