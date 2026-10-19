// 中文註解：這是位元組定位測試
module loc01_top;
	// 含 tab 與多位元組字元：αβγ 😀
	localparam string MSG = "你好，世界";
	logic [7:0]
		data_bus,
		data_aux;
	int	/* 位元組 😀 */	after_tab;   // 結尾註解
endmodule
