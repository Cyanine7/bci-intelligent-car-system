#include "usartx.h"
#include "project_link.h"
SEND_DATA Send_Data;
RECEIVE_DATA Receive_Data;
extern int Time_count;
/**************************************************************************
Function: Serial port 1 initialization
Input   : none
Output  : none
函数功能：串口1初始化
入口参数：无
返 回 值：无
**************************************************************************/
void uart1_init(u32 bound)
{  	 
	GPIO_InitTypeDef GPIO_InitStructure;
	USART_InitTypeDef USART_InitStructure;
	NVIC_InitTypeDef NVIC_InitStructure;
	
	RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOA, ENABLE);	 //Enable the gpio clock //使能GPIO时钟
	RCC_APB2PeriphClockCmd(RCC_APB2Periph_USART1, ENABLE); //Enable the Usart clock //使能USART时钟

	GPIO_PinAFConfig(GPIOA,GPIO_PinSource9,GPIO_AF_USART1);	
	GPIO_PinAFConfig(GPIOA,GPIO_PinSource10 ,GPIO_AF_USART1);	 
	
	GPIO_InitStructure.GPIO_Pin = GPIO_Pin_9|GPIO_Pin_10;
	GPIO_InitStructure.GPIO_Mode=GPIO_Mode_AF;            //输出模式
	GPIO_InitStructure.GPIO_OType=GPIO_OType_PP;          //推挽输出
	GPIO_InitStructure.GPIO_Speed=GPIO_Speed_50MHz;       //高速50MHZ
	GPIO_InitStructure.GPIO_PuPd=GPIO_PuPd_UP;            //上拉
	GPIO_Init(GPIOA, &GPIO_InitStructure);  		          //初始化
	
  //UsartNVIC configuration //UsartNVIC配置
	NVIC_InitStructure.NVIC_IRQChannel = USART1_IRQn;
	//Preempt priority //抢占优先级
	NVIC_InitStructure.NVIC_IRQChannelPreemptionPriority=3 ;
	//Subpriority //子优先级
	NVIC_InitStructure.NVIC_IRQChannelSubPriority = 3;		
	//Enable the IRQ channel //IRQ通道使能
	NVIC_InitStructure.NVIC_IRQChannelCmd = ENABLE;	
  //Initialize the VIC register with the specified parameters 
	//根据指定的参数初始化VIC寄存器	
	NVIC_Init(&NVIC_InitStructure);	
	
  //USART Initialization Settings 初始化设置
	USART_InitStructure.USART_BaudRate = bound; //Port rate //串口波特率
	USART_InitStructure.USART_WordLength = USART_WordLength_8b; //The word length is 8 bit data format //字长为8位数据格式
	USART_InitStructure.USART_StopBits = USART_StopBits_1; //A stop bit //一个停止位
	USART_InitStructure.USART_Parity = USART_Parity_No; //Prosaic parity bits //无奇偶校验位
	USART_InitStructure.USART_HardwareFlowControl = USART_HardwareFlowControl_None; //No hardware data flow control //无硬件数据流控制
	USART_InitStructure.USART_Mode = USART_Mode_Rx | USART_Mode_Tx;	//Sending and receiving mode //收发模式
	USART_Init(USART1, &USART_InitStructure); //Initialize serial port 1 //初始化串口1
	
	USART_ITConfig(USART1, USART_IT_RXNE, ENABLE); //Open the serial port to accept interrupts //开启串口接受中断
	USART_Cmd(USART1, ENABLE);                     //Enable serial port 1 //使能串口1
}
/**************************************************************************
Function: Serial port 2 initialization
Input   : none
Output  : none
函数功能：串口2初始化
入口参数：无
返回  值：无
**************************************************************************/
void uart2_init(u32 bound)
{  	 
	GPIO_InitTypeDef GPIO_InitStructure;
	USART_InitTypeDef USART_InitStructure;
	NVIC_InitTypeDef NVIC_InitStructure;

	RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOD, ENABLE);	 //Enable the gpio clock  //使能GPIO时钟
	RCC_APB1PeriphClockCmd(RCC_APB1Periph_USART2, ENABLE); //Enable the Usart clock //使能USART时钟
	
	GPIO_PinAFConfig(GPIOD,GPIO_PinSource5,GPIO_AF_USART2);	
	GPIO_PinAFConfig(GPIOD,GPIO_PinSource6 ,GPIO_AF_USART2);	 
	
	GPIO_InitStructure.GPIO_Pin = GPIO_Pin_5|GPIO_Pin_6;
	GPIO_InitStructure.GPIO_Mode=GPIO_Mode_AF;            //输出模式
	GPIO_InitStructure.GPIO_OType=GPIO_OType_PP;          //推挽输出
	GPIO_InitStructure.GPIO_Speed=GPIO_Speed_50MHz;       //高速50MHZ
	GPIO_InitStructure.GPIO_PuPd=GPIO_PuPd_UP;            //上拉
	GPIO_Init(GPIOD, &GPIO_InitStructure);  		          //初始化
	
	//UsartNVIC configuration //UsartNVIC配置
	NVIC_InitStructure.NVIC_IRQChannel = USART2_IRQn;
	//Preempt priority //抢占优先级
	NVIC_InitStructure.NVIC_IRQChannelPreemptionPriority=1 ;
	//Subpriority //子优先级
	NVIC_InitStructure.NVIC_IRQChannelSubPriority = 0;	
  //Enable the IRQ channel //IRQ通道使能	
	NVIC_InitStructure.NVIC_IRQChannelCmd = ENABLE;
  //Initialize the VIC register with the specified parameters 
	//根据指定的参数初始化VIC寄存器		
	NVIC_Init(&NVIC_InitStructure);	
	
	//USART Initialization Settings 初始化设置
	USART_InitStructure.USART_BaudRate = bound; //Port rate //串口波特率
	USART_InitStructure.USART_WordLength = USART_WordLength_8b; //The word length is 8 bit data format //字长为8位数据格式
	USART_InitStructure.USART_StopBits = USART_StopBits_1; //A stop bit //一个停止
	USART_InitStructure.USART_Parity = USART_Parity_No; //Prosaic parity bits //无奇偶校验位
	USART_InitStructure.USART_HardwareFlowControl = USART_HardwareFlowControl_None; //No hardware data flow control //无硬件数据流控制
	USART_InitStructure.USART_Mode = USART_Mode_Rx | USART_Mode_Tx;	//Sending and receiving mode //收发模式
	USART_Init(USART2, &USART_InitStructure);      //Initialize serial port 2 //初始化串口2
	
	USART_ITConfig(USART2, USART_IT_RXNE, ENABLE); //Open the serial port to accept interrupts //开启串口接受中断
	USART_Cmd(USART2, ENABLE);                     //Enable serial port 2 //使能串口2 
}

/**************************************************************************
Function: Serial port 1 receives interrupted
Input   : none
Output  : none
函数功能：串口1接收中断
入口参数：无
返 回 值：无
**************************************************************************/
int USART1_IRQHandler(void)
{	
	if(USART_GetITStatus(USART1, USART_IT_RXNE) != RESET) //Check if data is received //判断是否接收到数据
	{
		u8 Usart_Receive;		
		Usart_Receive = USART_ReceiveData(USART1); //Read the data //读取数据	
	}
	return 0;	
}
/**************************************************************************
Function: Jump to BootLoader via software reset.
Input   : none
Output  : none
函数功能：软复位进BootLoader区域
入口参数：无
返回  值：无
**************************************************************************/

/**************************************************************************
Function: Refresh the OLED screen
Input   : none
Output  : none
函数功能：串口2接收中断
入口参数：无
返回  值：无
**************************************************************************/
int USART2_IRQHandler(void)
{
    uint32_t status = USART2->SR;
    uint8_t byte;
    if(status & (USART_SR_RXNE | USART_SR_ORE | USART_SR_NE | USART_SR_FE | USART_SR_PE)) {
        byte = (uint8_t)USART2->DR;
        if(status & (USART_SR_ORE | USART_SR_NE | USART_SR_FE | USART_SR_PE))
            Project_RxError();
        else if(status & USART_SR_RXNE)
            Project_RxByte(byte);
    }
    if((USART2->CR1 & USART_CR1_TXEIE) && (USART2->SR & USART_SR_TXE)) {
        if(Project_TxByte(&byte)) USART2->DR=byte;
        else USART_ITConfig(USART2, USART_IT_TXE, DISABLE);
    }
    return 0;
}

/**************************************************************************
Function: Serial port 1 sends data
Input   : The data to send
Output  : none
函数功能：串口1发送数据
入口参数：要发送的数据
返回  值：无
**************************************************************************/
void usart1_send(u8 data)
{
	USART1->DR = data;
	while((USART1->SR&0x40)==0);	
}
/**************************************************************************
Function: Serial port 2 sends data
Input   : The data to send
Output  : none
函数功能：串口2发送数据
入口参数：要发送的数据
返回  值：无
**************************************************************************/
void usart2_send(u8 data)
{
	USART2->DR = data;
	while((USART2->SR&0x40)==0);	
}
//蓝牙AT指令抓包，防止指令干扰到机器人正常的蓝牙通信
u8 AT_Command_Capture(u8 uart_recv)
{
	/*
	蓝牙链接时发送的字符，00:11:22:33:44:55为蓝牙的MAC地址
	+CONNECTING<<00:11:22:33:44:55\r\n
	+CONNECTED\r\n
	共44个字符
	
	蓝牙断开时发送的字符
	+DISC:SUCCESS\r\n
	+READY\r\n
	+PAIRABLE\r\n
	共34个字符
	\r -> 0x0D
	\n -> 0x0A
	*/
	
	static u8 pointer = 0; //蓝牙接受时指针记录器
	static u8 bt_line = 0; //表示现在在第几行
	static u8 disconnect = 0;
	static u8 connect = 0;
	
	//断开连接
	static char* BlueTooth_Disconnect[3]={"+DISC:SUCCESS\r\n","+READY\r\n","+PAIRABLE\r\n"};
	
	//开始连接
	static char* BlueTooth_Connect[2]={"+CONNECTING<<00:00:00:00:00:00\r\n","+CONNECTED\r\n"};


	//特殊标识符，开始警惕(使用时要-1)
	if(uart_recv=='+') 
	{
		bt_line++,pointer=0; //收到‘+’，表示切换了行数	
		disconnect++,connect++;
		return 1;//抓包，禁止控制
	}

	if(bt_line!=0) 
	{	
		pointer++;

		//开始追踪数据是否符合断开的特征，符合时全部屏蔽，不符合时取消屏蔽
		if(uart_recv == BlueTooth_Disconnect[bt_line-1][pointer])
		{
			disconnect++;
			if(disconnect==34) disconnect=0,connect=0,bt_line=0,pointer=0;
			return 1;//抓包，禁止控制
		}			

		//追踪连接特征 (bt_line==1&&connect>=13)区段是蓝牙MAC地址，每一个蓝牙MAC地址都不相同，所以直接屏蔽过去
		else if(uart_recv == BlueTooth_Connect[bt_line-1][pointer] || (bt_line==1&&connect>=13) )
		{		
			connect++;
			if(connect==44) connect=0,disconnect=0,bt_line=0,pointer=0;		
			return 1;//抓包，禁止控制
		}	

		//在抓包期间收到其他命令，停止抓包
		else
		{
			disconnect = 0;
			connect = 0;
			bt_line = 0;		
			pointer = 0;
			return 0;//非禁止数据，可以控制
		}			
	}
	
	return 0;//非禁止数据，可以控制
}
