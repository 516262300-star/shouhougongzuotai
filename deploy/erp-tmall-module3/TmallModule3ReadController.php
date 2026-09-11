<?php

namespace App\Http\Controllers;

use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

/** 专用只读契约：不构造 AliController，不读取 showlist，不派任务，不调用退款。 */
class TmallModule3ReadController extends Controller
{
    public function inspect(Request $request)
    {
        $allowed = array_filter(array_map('trim', explode(',',
            (string) env('TMALL_MODULE3_READ_USERS', ''))));
        if (!in_array((string) session('username'), $allowed, true)) {
            return response()->json(['error' => 'module3_reader_not_allowed'], 403);
        }
        $order = (string) $request->query('order_id', '');
        if (!preg_match('/\A[0-9]{15,22}\z/', $order)) {
            return response()->json(['error' => 'invalid_order'], 422);
        }
        // 查同父订单所有平台记录，不只查目标refundId；不漏掉重开和重复售后。
        $rows = DB::table('refunds')->where('orderId', $order)->orderBy('id')
            ->limit(21)->get([
                'id', 'platform', 'orderId', 'refundId', 'overall_status', 'applyPayment',
                'applyCarriage', 'detail', 'ddnr', 'csname', 'isRefundGoods', 'waybill', 'log',
            ]);
        $activating = DB::table('00so')->where('客户编号', 'like', '%' . $order)->exists();
        $editing = DB::table('quotation')->where('客户编号', 'like', '%' . $order)
            ->where(function ($q) {
                $q->where('订单编号', 'like', '%X')->orWhere('订单编号', 'like', '%Y')
                    ->orWhere('状态', '<>', '');
            })->exists();
        return response()->json([
            'contract' => 'tmall_module3_read_v1',
            'order_id' => $order,
            'complete' => $rows->count() < 21,
            'activating' => $activating,
            'editing' => $editing,
            'records' => $rows,
        ])->header('Cache-Control', 'no-store, private');
    }
}
