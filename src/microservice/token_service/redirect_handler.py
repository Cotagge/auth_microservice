import requests
import json
import datetime
import base64
import jwt
import socket
import os
import stat
from urllib.parse import quote
from django.http import (
        HttpRequest,
        HttpResponse,
        HttpResponseBadRequest,
        HttpResponseRedirect,
        JsonResponse,
        HttpResponseServerError)
from django.core.exceptions import ObjectDoesNotExist
from django.utils.timezone import now
from .util import generate_nonce, list_subset, is_sock
from .config import Config


SOCK_DGRAM_LEN = 1024

'''
provides context management. only provides wait(int)
'''
class DomainSocketCondition(object):
    def __init__(self, path):
        print('initializing domain socket to: ' + path)
        self.path = path
    def acquire(self):
        print('acquiring lock on domain socket: ' + self.path)
        # check to see if the path is there
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)

    def release(self):
        print('releasing lock on domain socket: ' + self.path)
        self.sock.close()
        if stat.S_ISSOCK(os.stat(self.path).st_mode):
            os.unlink(self.path)
    
    def wait(self, seconds):
        print('waiting for domain socket condition on: ' + self.path)
        seconds = int(seconds) # let it fail
        self.sock.settimeout(seconds)
        data,address = self.sock.recvfrom(SOCK_DGRAM_LEN)
        if data:
            data = data.decode('utf-8') # check value, or not care
    
    def notify(self, msg='SUCCESS'):
        print('notifying observer on domain socket')
        cli_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            cli_sock.sendto(msg, self.path)
        finally:
            cli_sock.close()

    def __enter__(self):
        self.acquire()
    def __exit__(self, *args):
        self.release()


'''
    This is the top level handler of authorization redirects and authorization url generation.

    For non-standard APIs which do not conform to OAuth 2.0 (specifically RFC 6749 sec 4.1), extensions may be required.
    (RFC 6749 sec 4.1.1 https://tools.ietf.org/html/rfc6749#section-4.1.1)
    Example: Dropbox APIv2 does not completely conform to RFC 6749#4.1.1 (authorization) nor 6749#4.1.3 (token exchange)

    State/nonce values in the urls generated by this class must not be modified by the client application or end user.
    Requests received which do not match state/nonce values generated by this class will be rejected.

    Does not yet support webfinger OpenID issuer discovery/specification.
'''
#TODO SSL Cert verification on all https requests. Force SSL on urls.
# Attempt to autodetect cacert location based on os, otherwise pull Mozilla's https://curl.haxx.se/ca/cacert.pem
# also look at default ssl verification in requests package, and in pyoidc package, could rely on them
class RedirectHandler(object):

    def __init__(self):
        # TODO refactor so this lazy import is not needed
        from . import models
        globals()['models'] = models

        # timeout in seconds for authorization callbacks to be received
        # default is 300 (5 minutes)
        self.authorization_timeout = int(Config.get('authorization_timeout', 60*5))


    '''
        uid: unique user identifier
        scopes: iterable of strings, used by OAuth2 and OpenID. If requesting authentication
                    via an OpenID provider, this must include 'openid'.
        provider_tag: matched against provider dictionary keys in the configuration loaded at startup
    '''
    def add(self, uid, scopes, provider_tag, return_to=None):
        print('adding callback waiter with uid {}, scopes {}, provider {}, return_to {}'.format(uid, scopes, provider_tag, return_to))
        scopes = sorted(scopes)
        if uid == None:
            uid = ''
        if return_to == None:
            return_to = ''
        else:
            # santize input TODO what to do if no protocol specified
            return_to = return_to.strip('/')

        # enforce uniqueness of nonces
        # TODO cleanup old ones after authorization url expiration threshold
        while True:
            nonce = generate_nonce(64) # url safe 32byte (64byte hex)
            if self.is_nonce_unique(nonce):
                break
        while True:
            state = generate_nonce(64)
            if self.is_nonce_unique(state):
                break

        n_db = models.Nonce(value=nonce)
        n_db.save()
        s_db = models.Nonce(value=state)
        s_db.save()

        url = self._generate_authorization_url(state, nonce, scopes, provider_tag)
        pending = models.PendingCallback(
                uid=uid,
                state=state,
                nonce=nonce,
                provider=provider_tag,
                url=url,
                return_to=return_to
        )
        pending.save()
        # create scopes if not exist:
        for scope in scopes:
            s,created = models.Scope.objects.get_or_create(name=scope)
            pending.scopes.add(s)

        pending.save()

        return url, nonce


    def block(self, uid, scopes, provider_tag):
        scopes = sorted(scopes)

        pending_callbacks = []
        p_db = models.PendingCallback.objects.all()
        for p in p_db:
            if p.uid == uid and list_subset(scopes, p.scopes.all()) and p.provider == provider_tag:
                pending_callbacks.append(p)
        if len(pending_callbacks) == 0:
            return None # no pending callback found, must re-authorize from scratch by calling add

        import tempfile
        tmpdir = tempfile.mkdtemp()
        tmpfile = tmpdir + '/' + generate_nonce(32)
        lock = DomainSocketCondition(tmpfile)

        observer = models.BlockingRequest(
                uid=uid,
                provider=provider,
                socket_file=tmpfile,
                nonce=None
        )
        observer.save()
        for scope in scopes:
            s_db = models.Scope.objects.get_or_create(name=scope.name)
            observer.scopes.add(s_db)

        observer.save()
        return lock

    def block_nonce(self, nonce):
        pending_callbacks = []
        p_db = models.PendingCallback.objects.all()
        for p in p_db:
            if p.nonce == nonce:
                pending_callbacks.append(p)
        if len(pending_callbacks) == 0:
            return None # no pending callback found, must re-authorize from scratch by calling add
        if len(pending_callbacks) != 1:
            raise RuntimeError('multiple pending callbacks with same nonce, cannot proceed')
        p = pending_callbacks[0]

        import tempfile
        tmpdir = tempfile.mkdtemp()
        tmpfile = tmpdir + '/' + generate_nonce(32)
        lock = DomainSocketCondition(tmpfile)
                
        observer = models.BlockingRequest(
                uid=p.uid,
                provider=p.provider,
                socket_file=tmpfile,
                nonce=nonce
        )
        observer.save()
        for scope in p.scopes.all():
            observer.scopes.add(scope)

        observer.save()
        return lock


    '''
        Accept a request conforming to Authorization Code Flow OIDC Core 1.0 section 3.1.2.5
        (http://openid.net/specs/openid-connect-core-1_0.html#AuthResponse)

        request is a django.http.HttpRequest

        Returns an HttpResponse, with corresponding values filled in for client
    '''
    def accept(self, request):
        state = request.GET.get('state')
        code = request.GET.get('code')
        if not code:
            return HttpResponseBadRequest('callback did not contain an authorization code')
        if not state:
            return HttpResponseBadRequest('callback state did not match expected')
        
        w = self.get_pending_by_state(state)
        if not w:
            return HttpResponseBadRequest('callback request from login is malformed, or authorization session expired')
        else:
            provider = w.provider
            if self.is_openid(provider):
                meta = models.OIDCMetadataCache.objects.get(provider=provider).value
                meta = json.loads(meta)
                token_endpoint = meta['token_endpoint']
            else: # require non-openid providers to specify the token endpoint
                token_endpoint = Config['providers'][provider]['token_endpoint']
            client_id = Config['providers'][provider]['client_id']
            client_secret = Config['providers'][provider]['client_secret']
            redirect_uri = Config['redirect_uri']
            token_response = self._token_request(
                    token_endpoint,
                    client_id,
                    client_secret,
                    code,
                    redirect_uri)
            if token_response.status_code not in [200,302]:
                return HttpResponseServerError('could not acquire token from provider' + str(vars(token_response)))
            
            if provider == 'globus':
                (success,msg,user,token,nonce) = GlobusRedirectHandler()._handle_token_response(w, token_response)
            else:
                (success,msg,user,token,nonce) = self._handle_token_response(w, token_response)
            
            if not success:
                return HttpResponseServerError(msg + ':' + token_response)

            # notify anyone blocking for (uid,scopes,provider) token criteria
            blocking_requests = []
            b_db = models.BlockingRequest.objects.all()
            for b in b_db:
                blocking_scopes = [s.name for s in b.scopes.all()]
                waiting_scopes = [s.name for s in w.scopes.all()]
                # if the scopes that a client is blocking for is a subset of the scopes from this callback
                if b.uid == user.id and list_subset(blocking_scopes, waiting_scopes) and b.provider == provider:
                    blocking_requests.append(b) 
            # should only be one which matches, but just in case...
            for b in blocking_requests:
                if is_sock(b.socket_file):
                    cli_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                    print('writing SUCCESS to domain socket: ' + b.socket_file)
                    try:
                        cli_sock.sendto('SUCCESS'.encode('utf-8'), b.socket_file)
                    except ConnectionRefusedError:
                        print('no one listening to socket')
                        os.unlink(b.socket_file)
                else:
                    print('orphaned blocking entry found: ' + b.socket_file)
                b.delete() # delete from db
                

            # notify anyone blocking for the nonce
            blocking_requests = []
            b_db = models.BlockingRequest.objects.all()
            for b in b_db:
                if b.nonce == nonce:
                    blocking_requests.append(b)
            # should only be one which matches, but just in case...
            for b in blocking_requests:
                if is_sock(b.socket_file):
                    cli_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                    print('writing SUCCESS to domain socket: ' + b.socket_file)
                    try:
                        cli_sock.sendto('SUCCESS'.encode('utf-8'), b.socket_file)
                    except ConnectionRefusedError:
                        print('no one listening to socket')
                        os.unlink(b.socket_file)
                else:
                    print('orphaned blocking entry found: ' + b.socket_file)
                b.delete() # delete from db

            if w.return_to:
                ret = HttpResponseRedirect(
                        '{}/?access_token={}&uid={}'.format(w.return_to, token.access_token, user.id))
            else:
                ret = HttpResponse('Successfully authenticated user')

            w.delete()
            return ret


    '''
    Called upon successful exhance of an authorization code for an access token. Takes a requests.models.Response object
    and w, a token_service.models.PendingCallback object
    returns (bool,message) or raises exception.
    '''
    def _handle_token_response(self, w, response):
        body = json.loads(response.content)
        id_token = body['id_token']
        access_token = body['access_token']
        expires_in = body['expires_in']
        refresh_token = body['refresh_token']
        print('token_response:\n' + str(body))
        # convert expires_in to timestamp
        expire_time = now() + datetime.timedelta(seconds=expires_in)
        #expire_time = expire_time.replace(tzinfo=datetime.timezone.utc)

        # expand the id_token to the encoded json object
        # TODO signature validation if signature provided
        id_token = jwt.decode(id_token, verify=False)
        print('id_token body:\n' + str(id_token))

        sub = id_token['sub']
        issuer = id_token['iss']
        nonce = id_token['nonce']
        if nonce != w.nonce:
            return (False,'login request malformed or expired',None,None,None)

        # check if user exists
        users = models.User.objects.filter(id=sub)
        if len(users) == 0:
            print('creating new user with id: {}'.format(sub))
            # try to fill username with email
            if 'email' in id_token:
                user_name = id_token['email']
            else:
                user_name = ''
                print('no email received for unrecognized user callback, filling user_name with blank string')
            user = models.User(
                    id=sub,
                    user_name=user_name)
            user.save()
        else:
            print('user recognized with id: {}'.format(sub))
            user = users[0]

        token = models.Token(
                user=user,
                access_token=access_token,
                refresh_token=refresh_token, #TODO what if no refresh_token in response
                expires=expire_time,
                provider=w.provider,
                issuer=issuer,
                enabled=True,
        )
        token.save()

        n,created = models.Nonce.objects.get_or_create(value=nonce)
        token.nonce.add(n)

        # link scopes, create if not exist:
        for scope in w.scopes.all():
            s,created = models.Scope.objects.get_or_create(name=scope.name)
            token.scopes.add(s)
        
        return (True,'',user,token,nonce)


    '''
        Performs the request to the token endpoint and returns a response object from the requests library
        
        Token endpoint MUST be TLS, because client secret is sent combined with client id in the authorization header.
        Client id is also sent as a parameter, because some APIs want that.
    '''
    def _token_request(self, token_endpoint, client_id, client_secret, code, redirect_uri):
        
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri,
            'access_type': 'offline',
            'client_id': client_id
        }

        # set up headers and send request. Return raw requests response
        authorization = base64.b64encode((client_id + ':' + client_secret).encode('utf-8'))
        headers = {
                'Authorization': 'Basic ' + str(authorization.decode('utf-8')),
                'Content-Type': 'application/x-www-form-urlencoded'
        }
        response = requests.post(token_endpoint, headers=headers, data=data)
        return response

    def _refresh_token(self, token_model):
        provider_config = Config['providers'][token_model.provider]
        if self.is_openid(token_model.provider):
            meta = self._get_or_update_OIDC_cache(token_model.provider)
            token_endpoint = meta['token_endpoint']
        elif self.is_oauth2(token_model.provider):
            token_endpoint = provider_config['token_endpoint']
        else:
            raise RuntimeError('could not refresh unrecognized provider standard')
        
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': token_model.refresh_token
        }
        client_id = provider_config['client_id']
        client_secret = provider_config['client_secret']
        authorization = base64.b64encode((client_id + ':' + client_secret).encode('utf-8'))
        headers = {
                'Authorization': 'Basic ' + str(authorization.decode('utf-8')),
                'Content-Type': 'application/x-www-form-urlencoded'
        }
        response = requests.post(token_endpoint, headers=headers, data=data)
        if response.status_code != 200:
            raise RuntimeError('could not refresh token, provider returned: {}\n{}'.format(response.status_code,response.content))
        else:
            content = response.content.decode('utf-8')
            obj = json.loads(content)
            if 'access_token' not in obj or 'expires_in' not in obj or 'token_type' not in obj:
                raise RuntimeError('refresh response missing required fields: {}\n{}'.format(response.status, str(obj)))
            token_model.expires = now() + datetime.timedelta(seconds=int(obj['expires_in']))
            token_model.access_token = obj['access_token']
            if 'refresh_token' in obj:
                token_model.refresh_token = obj['refresh_token']
            token_model.save()
            return token_model

    def is_openid(self, provider):
        return Config['providers'][provider]['standard'] == 'OpenID Connect'
    def is_oauth2(self, provider):
        return Config['providers'][provider]['standard'] == 'OAuth 2.0'
    
    def is_nonce_unique(self, nonce):
        # TODO update with https://github.com/heliumdatacommons/auth_microservice/issues/4 when resolved
        queryset = models.Nonce.objects.all()
        for n in queryset:
            if n.value == nonce:
                return False
        return True

    def get_pending_by_state(self, state):
        l = self.get_pending_by_field('state', state)
        if len(l) != 1: return None
        else: return l[0]

    def get_pending_by_nonce(self, nonce):
        l = self.get_pending_by_field('nonce', nonce)
        if len(l) != 1: return None
        else: return l[0]

    def get_pending_by_field(self, fieldname, fieldval):
        # TODO update with native encrypted filtering
        queryset = models.PendingCallback.objects.all()
        l = []
        for q in queryset:
            if getattr(q, fieldname) == fieldval:
                l.append(q)
        return l
    
    def _get_or_update_OIDC_cache(self, provider_tag):
        provider_config = Config['providers'][provider_tag]
        meta_url = provider_config['metadata_url']
        cache = models.OIDCMetadataCache.objects.filter(provider=provider_tag)
        if cache.count() == 0 or (cache[0].retrieval_time + datetime.timedelta(hours=24)) < now():
            # not cached, or cached entry is more than 1 day old 
            response = requests.get(meta_url)
            if response.status_code != 200:
                raise RuntimeError('could not retrieve openid metadata, returned error: ' 
                        + str(response.status_code) + '\n' + response.content.decode('utf-8'))
            content = response.content.decode('utf-8')
            meta = json.loads(content)
            # cache this metadata
            if cache.count() == 0: # create
                models.OIDCMetadataCache.objects.create(provider=provider_tag, value=content)
            else: # update
                cache[0].value=content
                cache[0].save()
        else:
            meta = json.loads(cache[0].value)
        return meta
 
    '''
        Create a proper authorization url based on provided parameters
    '''
    def _generate_authorization_url(self, state, nonce, scopes, provider_tag):
        provider_config = Config['providers'][provider_tag]
        client_id = provider_config['client_id']
        redirect_uri = Config['redirect_uri']

        # get auth endpoint
        if self.is_openid(provider_tag):
            # openid allowed for endpoint and other value specification within metadata file
            meta_url = provider_config['metadata_url']

            meta = self._get_or_update_OIDC_cache(provider_tag)

            authorization_endpoint = meta['authorization_endpoint']
            scope = ' '.join(scopes)
            scope = quote(scope)
            additional_params = 'scope=' + scope
            additional_params += '&response_type=code'
            additional_params += '&access_type=offline'
            additional_params += '&login%20consent'

        elif self.is_oauth2(provider_tag):
            authorization_endpoint = provider_config['authorization_endpoint']
            additional_params = ''
            if 'additional_params' in provider_config:
                additional_params = provider_config['additional_params']

        else:
            raise RuntimeError('unknown provider standard: ' + provider_config['standard'])

        url = '{}?nonce={}&state={}&redirect_uri={}&client_id={}&{}'.format(
            authorization_endpoint,
            nonce,
            state,
            redirect_uri,
            client_id,
            additional_params,
        )
        return url

'''
Almost everything is the same for Globus, except when user authorizes scopes which span resource servers, there
is an access token returned per-server, instead of one which encompasses all of the scopes.
'''
class GlobusRedirectHandler(RedirectHandler):
    '''
    allow RedirectHandler to do everything except the parsing and handling of the token response
    this also differs from RedirectHandler._handle_token_response because there can be multiple tokens in the callback
    request. This method returns as the token return object, the top level token in the response, but also stores the 'other_tokens'
    '''
    def _handle_token_response(self, w, response):
        body = json.loads(response.content)
        tokens = []
        user = token = nonce = None
        # check to see if top level token is for openid
        if 'openid' in body['scope'] and 'id_token' in body:
            success,msg,user,token,nonce = super()._handle_token_response(w, response)
            if not success:
                return (success,msg,user,token,nonce)
            tokens.append(token)
        else:
            # check if user exists
            if not user: # no openid token was in this response
                users = models.User.objects.filter(id=w.uid)
                if len(users) > 0:
                    user = users[0]
                else:
                    # only thing we have here is the subject id, so use sub id as the user_name too
                    user_name = w.uid
                    print('unrecognized user for Globus token response without an id_token field,'
                            + 'filling user_name with the same as the id')
                    user = models.User.objects.create(
                            id=w.uid,
                            user_name=user_name)
                    user.save()
            # For globus, on a token callback it also puts the state value into the root level
            # json object. This is actually pretty nice and should be part of the OAuth2.0 spec.
            # However substituting the state value for the nonce (in OAuth2 callbacks, not OIDC)
            # will break our ability to let clients block based on the initial nonce parameter
            # sent in the original authorization url. Use the nonce in the PendingCallback object
            # and link the tokens to it, even if the nonce was not returned to us in the token
            # callback from globus
            if not nonce:
                nonce = w.nonce

            success,msg,user,token,nonce = self._handle_token_body(user, w, nonce, body)
            if not success:
                return (success,msg,user,token,nonce)
            tokens.append(token)

        # check if user exists
        if not user: # no openid token was in this response
            users = models.User.objects.filter(id=w.uid)
            if len(users) > 0:
                user = users[0]
            else:
                # only thing we have here is the subject id, so use sub id as the user_name too
                user_name = w.uid
                print('unrecognized user for Globus token response without an id_token field,'
                        + 'filling user_name with the same as the id')
                user = models.User.objects.create(
                        id=w.uid,
                        user_name=user_name)
                user.save()

        if 'other_tokens' in body and len(body['other_tokens']) > 0:
            for other_token in body['other_tokens']:
                success,msg,user,token,nonce = self._handle_token_body(user, w, nonce, other_token)
                tokens.append(token)
                
        return (True,'',user,tokens[0],nonce)

    def _handle_token_body(self, user, w, nonce, token_dict):
        print('handling globus token body:\n' + str(token_dict))
        access_token = token_dict['access_token']
        expires_in = token_dict['expires_in']
        refresh_token = token_dict['refresh_token']
        provider = w.provider

        # convert expires_in to timestamp
        expire_time = now() + datetime.timedelta(seconds=expires_in)

        token = models.Token(
                user=user,
                access_token=access_token,
                refresh_token=refresh_token, #TODO what if no refresh_token in response
                expires=expire_time,
                provider=provider,
                issuer=token_dict['resource_server'],
                enabled=True,
        )
        token.save()

        n,created = models.Nonce.objects.get_or_create(value=nonce)
        token.nonce.add(n)

        # link scopes, create if not exist:
        #for scope in w.scopes.all():
        if isinstance(token_dict['scope'], str):
            s,created = models.Scope.objects.get_or_create(name=token_dict['scope'])
            token.scopes.add(s)
        return (True, '', user, token, nonce)

