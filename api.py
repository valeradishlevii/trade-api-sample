import datetime

from django.forms.models import model_to_dict
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from rest_framework.reverse import reverse
from rest_framework.generics import GenericAPIView

from trade.brokers.goptions import GOptions, NoResults
from trade.models import Broker, Instrument, InstrumentBrokerData

from .serializers import AuthSerializer, InstrumentSerializer, SetPositionSerializer, AllInstrumentSerializer


def need_auth(fn):
    def wrapped(self, request, *args, **kwargs):
        if 'customer_id' not in request.session:
            return Response({'error': 'Not authorized'}, status=status.HTTP_401_UNAUTHORIZED)
        return fn(self, request, *args, **kwargs)
    return wrapped


@api_view(['GET'])
def api_root(request, format=None):
    """
    The entry endpoint of our API.
    """
    return Response({
        'auth': reverse('api-auth', request=request),
        'auth-check': reverse('api-auth-check', request=request),
        'profile': reverse('api-profile', request=request),
        'positions-open': reverse('api-positions-open', request=request),
        'positions-closed': reverse('api-positions-closed', request=request),
        'instruments': reverse('api-instruments', request=request),
        'options': reverse('api-options', request=request),
        'trade': reverse('api-trade', request=request),
        'rate-history': reverse('api-rate-history', request=request),
        'rate-last': reverse('api-rate-last', request=request),
    })


class AuthUser(GenericAPIView):
    serializer_class = AuthSerializer

    def post(self, request, *args, **kwargs):
        if 'customer_id' in request.session:
            del request.session['customer_id']
        serializer = self.get_serializer(data=request.DATA, files=request.FILES)
        if serializer.is_valid():
            try:
                g = GOptions()
                doc, raw_result = g.callAPI({
                    'MODULE': 'Customer',
                    'COMMAND': 'view',
                    'FILTER[email]': serializer.object['email'],
                    'FILTER[password]': serializer.object['password'],
                })
                customer = g._dictValue(doc.getElementsByTagName('Customer')[0].childNodes[0])
                request.session['customer_id'] = customer['id']
                return Response({'result': 'Authorized'})
            except Exception, e:
                return Response({'error': 'Wrong credentials'}, status=status.HTTP_401_UNAUTHORIZED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class IsUserAuthorized(GenericAPIView):
    def get(self, request, *args, **kwargs):
        is_auth = 'customer_id' in request.session
        return Response({'authorized': is_auth})


class ProfileUser(GenericAPIView):
    @need_auth
    def get(self, request, *args, **kwargs):
        try:
            g = GOptions()
            doc, raw_result = g.callAPI({
                'MODULE': 'Customer',
                'COMMAND': 'view',
                'FILTER[id]': request.session['customer_id'],
            })
            customer = g._dictValue(doc.getElementsByTagName('Customer')[0].childNodes[0])
            return Response({
                'profile': {
                    'name': customer['FirstName'],
                    'account_balance': float(customer['accountBalance']),
                    'currency': customer['currency'],
                }
            })
        except Exception, e:
            return Response({'error': 'Something went wrong'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UserOpenPositionList(GenericAPIView):
    @need_auth
    def get(self, request, *args, **kwargs):
        try:
            position_list = []
            g = GOptions()
            try:
                doc, raw_result = g.callAPI({
                    'MODULE': 'Positions',
                    'COMMAND': 'view',
                    'FILTER[customerId]': request.session['customer_id'],
                    'FILTER[status]': 'open',
                })

                # position_list = g.get_customer_positions(request.session['customer_id'])
                position_list = []
                for position in doc.getElementsByTagName('Positions')[0].childNodes:
                    pos = g._dictValue(position)
                    position_list.append({
                        'asset_name': pos['name'],
                        'asset_id': int(pos['assetId']),
                        'asset_class': InstrumentBrokerData.objects.get(external_id=int(pos['assetId'])).instrument.asset_class,
                        'open_date': pos['executionDate'],  # can use 'date' but wrong format
                        'open_rate': float(pos['entryRate']),
                        'close_date': pos['optionEndDate'],
                        'amount': int(float(pos['amount'])),
                        'currency': pos['currency'],
                        'position': pos['position'],
                        'potential_payout': float(pos['winSum']),
                    })
            except NoResults:
                pass

            return Response({
                'asset_classes': dict(Instrument.ASSET_CLASSES),
                'position_list': position_list
            })
        except:
            return Response({'error': 'Something went wrong'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UserClosedPositionList(GenericAPIView):
    @need_auth
    def get(self, request, *args, **kwargs):
        try:
            position_list = []
            min_date = datetime.datetime.utcnow() - datetime.timedelta(days=7)
            g = GOptions()
            try:
                doc, raw_result = g.callAPI({
                    'MODULE': 'Positions',
                    'COMMAND': 'view',
                    'FILTER[customerId]': request.session['customer_id'],
                    'FILTER[date][min]': min_date.strftime("%Y-%m-%d %H:%M:%S")
                })

                # position_list = g.get_customer_positions(request.session['customer_id'])
                for position in doc.getElementsByTagName('Positions')[0].childNodes:
                    pos = g._dictValue(position)
                    if pos['status'] == 'open':
                        continue
                    position_list.append({
                        'asset_name': pos['name'],
                        'asset_id': int(pos['assetId']),
                        'asset_class': InstrumentBrokerData.objects.get(external_id=int(pos['assetId'])).instrument.asset_class,
                        'open_date': pos['executionDate'],  # can use 'date' but wrong format
                        'open_rate': float(pos['entryRate']),
                        'close_date': pos['optionEndDate'],
                        'close_rate': float(pos['endRate']),
                        'amount': int(float(pos['amount'])),
                        'currency': pos['currency'],
                        'position': pos['position'],
                        'payout': float(pos['payout']),
                        'status': pos['status'],
                    })
            except NoResults:
                pass

            return Response({
                'asset_classes': dict(Instrument.ASSET_CLASSES),
                'position_list': position_list
            })
        except:
            return Response({'error': 'Something went wrong'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _get_tradeable_instruments():
    g = GOptions()
    instrument_ids, option_list = g.get_available_options()
    broker = Broker.objects.get(name='GOptions')
    instrument_list = []
    for external_id in instrument_ids:
        instrument = InstrumentBrokerData.objects.get(broker=broker, external_id=external_id).instrument
        instrument_dict = model_to_dict(instrument)
        instrument_dict['external_id'] = external_id
        instrument_list.append(instrument_dict)
        for option in option_list:
            if option['assetId'] == external_id:
                option['instrument_pk'] = instrument.pk
    instrument_list.sort(key=lambda x: x['name'])
    return instrument_list, option_list


class InstrumentList(GenericAPIView):
    serializer_class = AllInstrumentSerializer

    @need_auth
    def get(self, request, *args, **kwargs):
        try:
            instrument_classes = Instrument.ASSET_CLASSES
            instrument_list, option_list = _get_tradeable_instruments()

            return Response({
                'asset_classes': dict(instrument_classes),
                'instrument_list': [{
                    'id': p['id'],
                    'asset_class': p['asset_class'],
                    'name': p['name'],
                    'symbol': p['symbol'],
                } for p in instrument_list],
            })
        except:
            return Response({'error': 'Something went wrong'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @need_auth
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.DATA, files=request.FILES)
        if serializer.is_valid():
            if serializer.object['all_instruments']:
                instrument_classes = Instrument.ASSET_CLASSES
                instrument_list = list(Instrument.objects.all())
                instrument_list.sort(key=lambda x: x.name)

                return Response({
                    'asset_classes': dict(instrument_classes),
                    'instrument_list': [{
                        'id': p.pk,
                        'asset_class': p.asset_class,
                        'name': p.name,
                        'symbol': p.symbol,
                    } for p in instrument_list],
                })
            else:
                return self.get(request, *args, **kwargs)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class OptionList(GenericAPIView):
    serializer_class = InstrumentSerializer

    @need_auth
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.DATA, files=request.FILES)
        if serializer.is_valid():
            try:
                g = GOptions()
                broker = Broker.objects.get(name='GOptions')
                instrument = Instrument.objects.get(pk=serializer.object['instrument_id'])
                external_id = InstrumentBrokerData.objects.get(broker=broker, instrument=instrument).external_id
                instrument_ids, option_list = g.get_available_options(external_id)

                # Show only active options
                option_list = [o for o in option_list
                               if datetime.datetime.strptime(o['endDate'], "%Y-%m-%d %H:%M:%S") -
                                  datetime.timedelta(minutes=int(o['lastPositionTime'])) > datetime.datetime.utcnow()]

                return Response({
                    'option_list': [{
                        'id': int(o['id']),
                        'close_date': datetime.datetime.strptime(o['endDate'], "%Y-%m-%d %H:%M:%S").strftime("%H:%M %d/%m/%Y"),
                        'no_position_time': int(o['lastPositionTime']),
                        'profit': int(o['profit']),
                        'rule_id': int(o['ruleId']),
                        'asset_id': int(external_id),
                    } for o in option_list]
                })
            except:
                return Response({'error': 'Something went wrong'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SetPosition(GenericAPIView):
    serializer_class = SetPositionSerializer

    @need_auth
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.DATA, files=request.FILES)
        if serializer.is_valid():
            try:
                g = GOptions()
                broker = Broker.objects.get(name='GOptions')
                instrument = Instrument.objects.get(pk=serializer.object['instrument_id'])
                external_id = InstrumentBrokerData.objects.get(broker=broker, instrument=instrument).external_id
                success, data = g.add_position(
                    customer_id=request.session['customer_id'],
                    position=dict(Instrument.TYPES)[int(serializer.object['position'])].lower(),
                    amount=serializer.object['amount'],
                    option_id=serializer.object['option_id'],
                    asset_id=external_id,
                    rule_id=serializer.object['rule_id'],
                )
                return Response({
                    'success': success,
                    'rate': float(data.get('rate', 0))
                })
            except Exception, e:
                return Response({'error': 'Something went wrong', 'e': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RateHistory(GenericAPIView):
    serializer_class = InstrumentSerializer

    @need_auth
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.DATA, files=request.FILES)
        if serializer.is_valid():
            try:
                broker = Broker.objects.get(name='GOptions')
                instrument = Instrument.objects.get(pk=serializer.object['instrument_id'])
                external_id = InstrumentBrokerData.objects.get(broker=broker, instrument=instrument).external_id
                g = GOptions()
                rate_history = g.get_asset_history(external_id)

                return Response({
                    'rate_history': [{
                        'date': datetime.datetime.fromtimestamp(r[0]).strftime("%H:%M %d/%m/%Y"),
                        'timestamp': r[0],
                        'rate': r[1],
                    } for r in rate_history]
                })
            except:
                return Response({'error': 'Something went wrong'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RateLast(GenericAPIView):
    serializer_class = InstrumentSerializer

    @need_auth
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.DATA, files=request.FILES)
        if serializer.is_valid():
            try:
                broker = Broker.objects.get(name='GOptions')
                instrument = Instrument.objects.get(pk=serializer.object['instrument_id'])
                external_id = InstrumentBrokerData.objects.get(broker=broker, instrument=instrument).external_id
                g = GOptions()
                rate = g.get_last_rate(external_id)

                return Response({
                    'rate': rate
                })
            except:
                return Response({'error': 'Something went wrong'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
